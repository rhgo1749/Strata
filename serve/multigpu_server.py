"""Experimental 3-GPU Strata supervisor with a shared host expert arena.

This keeps Strata's existing single-GPU engine intact and composes several engine/server
processes into independent request lanes:

* each lane sees exactly one CUDA device (CUDA_VISIBLE_DEVICES)
* the resident expert arena is backed by one MAP_SHARED file (fork patch in pinned.cu)
* each lane keeps its own dense/QSA/MTP weights, hot-expert VRAM cache, and KV-resident window
* the full KV stays in host RAM through Strata's existing --kv-resident path
* PLE remains Strata's existing direct/O_DIRECT SSD reader

`--kv-budget` is a total capacity guard.  The first implementation partitions that
capacity between lanes; it is not yet a live cross-lane paged-KV allocator.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
GENERATE_PATHS = {"/v1/chat/completions", "/v1/messages"}


def option_value(args: list[str], name: str) -> str | None:
    try:
        i = args.index(name)
    except ValueError:
        return None
    return args[i + 1] if i + 1 < len(args) else None


def replace_option(args: list[str], name: str, value: int | str) -> list[str]:
    out = list(args)
    try:
        i = out.index(name)
    except ValueError:
        out += [name, str(value)]
        return out
    if i + 1 >= len(out):
        raise ValueError(f"{name} has no value in the base config")
    out[i + 1] = str(value)
    return out


def remove_option(args: list[str], name: str) -> list[str]:
    """Remove every `name value` pair; malformed trailing occurrences are removed too."""
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == name:
            i += 2 if i + 1 < len(args) else 1
            continue
        out.append(args[i])
        i += 1
    return out


def remove_flag(args: list[str], name: str) -> list[str]:
    """Remove every standalone flag occurrence."""
    return [arg for arg in args if arg != name]


def sanitize_lane_config(cfg: dict) -> dict:
    """Return a lane-local config that cannot re-expand itself into upstream layer-split mode."""
    lane_cfg = copy.deepcopy(cfg)
    lane_cfg.pop("gpu", None)
    lane_cfg.pop("layer_split", None)
    if isinstance(lane_cfg.get("args"), list):
        lane_cfg["args"] = remove_option(lane_cfg["args"], "--layer-split")
    return lane_cfg


def apply_vision_capability(lane_cfg: dict, enabled: bool) -> dict:
    """Strip encoder config and its reserved VRAM from lanes that are not vision-capable."""
    if enabled or not lane_cfg.get("vision"):
        return lane_cfg
    lane_cfg.pop("vision", None)
    if isinstance(lane_cfg.get("args"), list):
        lane_cfg["args"] = remove_flag(lane_cfg["args"], "--vision")
        lane_cfg["args"] = remove_option(lane_cfg["args"], "--vram-reserve-mib")
    return lane_cfg


def resolve_config_path(value: str, cwd: str | None) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = Path(cwd or ROOT) / p
    return p.resolve()


@dataclass(frozen=True)
class ArenaSpec:
    bytes: int
    expert_bytes: int
    max_blob: int
    n_expert: int


def native_arena_spec(pack: Path) -> ArenaSpec:
    """Return exactly the allocation ArenaExpertSource asks PinnedArena for."""
    meta = pack / "native_experts.txt"
    text = meta.read_text(encoding="utf-8")
    m = re.search(r"\bn_expert\s+(\d+)", text)
    n_expert = int(m.group(1)) if m else 512
    total = 0
    max_blob = 0
    rows = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 5:
            raise ValueError(f"{meta}: malformed native expert row: {raw!r}")
        off, blob = int(f[3]), int(f[4])
        if off < 0 or blob <= 0:
            raise ValueError(f"{meta}: invalid offset/blob in row: {raw!r}")
        total = max(total, off + blob * n_expert)
        max_blob = max(max_blob, blob)
        rows += 1
    if rows == 0 or total == 0 or max_blob == 0:
        raise ValueError(f"{meta}: no native expert rows")
    return ArenaSpec(total + max_blob, total, max_blob, n_expert)


def parse_int_list(text: str, *, what: str) -> list[int]:
    try:
        values = [int(x.strip()) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError(f"{what} must be a comma-separated integer list") from e
    if not values or any(v <= 0 for v in values):
        raise ValueError(f"{what} must contain positive integers")
    return values


def parse_float_list(text: str, *, what: str) -> list[float]:
    try:
        values = [float(x.strip()) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError(f"{what} must be a comma-separated numeric list") from e
    if not values or any(v < 0.0 or v > 1.0 for v in values):
        raise ValueError(f"{what} values must be between 0 and 1")
    return values


def parse_lane_indices(text: str, lanes: int, *, what: str) -> set[int]:
    if text.strip().lower() == "none":
        return set()
    try:
        values = [int(x.strip()) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError(f"{what} must be a comma-separated lane-index list or 'none'") from e
    if not values:
        raise ValueError(f"{what} must name at least one lane or 'none'")
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must not contain duplicate lane indices")
    if any(v < 0 or v >= lanes for v in values):
        raise ValueError(f"{what} lane indices must be between 0 and {lanes - 1}")
    return set(values)


def request_has_images(body: bytes) -> bool:
    """Classify supported OpenAI/Anthropic message bodies without interpreting image contents."""
    try:
        req = json.loads(body) if body else {}
    except (TypeError, ValueError):
        return False
    messages = req.get("messages") if isinstance(req, dict) else None
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                return True
    return False


def lane_contexts(base_context: int, lanes: int, requested: str | None, kv_budget: int | None) -> list[int]:
    if requested:
        values = parse_int_list(requested, what="--lane-contexts")
        if len(values) != lanes:
            raise ValueError(f"--lane-contexts has {len(values)} values for {lanes} GPU lanes")
    elif kv_budget is not None:
        q, r = divmod(kv_budget, lanes)
        values = [q + (1 if i < r else 0) for i in range(lanes)]
    else:
        values = [base_context] * lanes
    if kv_budget is not None and sum(values) > kv_budget:
        raise ValueError(
            f"lane contexts total {sum(values)} tokens, above --kv-budget {kv_budget}"
        )
    return values


def physical_core_cpu_sets() -> list[tuple[int, ...]]:
    """Allowed logical CPUs grouped by physical core, in the process's current affinity order."""
    if not hasattr(os, "sched_getaffinity"):
        return []
    allowed = sorted(os.sched_getaffinity(0))
    groups: dict[tuple[int, int], list[int]] = {}
    for cpu in allowed:
        topo = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            package = int((topo / "physical_package_id").read_text().strip())
            core = int((topo / "core_id").read_text().strip())
            key = (package, core)
        except (OSError, ValueError):
            key = (0, cpu)
        groups.setdefault(key, []).append(cpu)
    return [tuple(v) for v in groups.values()]


def partition_cpu_sets(core_groups: list[tuple[int, ...]], lanes: int) -> list[tuple[int, ...]]:
    """Round-robin physical cores across lanes so SMT siblings always stay together."""
    if lanes < 1:
        raise ValueError("lane count must be positive")
    if len(core_groups) < lanes * 2:
        raise ValueError(
            f"--cpu-partition auto needs at least two physical cores per lane; "
            f"found {len(core_groups)} cores for {lanes} lanes"
        )
    out: list[list[int]] = [[] for _ in range(lanes)]
    for i, group in enumerate(core_groups):
        out[i % lanes].extend(group)
    return [tuple(sorted(x)) for x in out]


def partition_cpu_sets_exact(core_groups: list[tuple[int, ...]], counts: list[int]) -> list[tuple[int, ...]]:
    """Spread an exact physical-core budget across lanes while keeping SMT siblings together."""
    if not counts or any(x < 2 for x in counts):
        raise ValueError("--lane-cpu-cores needs at least two physical cores per lane")
    if sum(counts) != len(core_groups):
        raise ValueError(
            f"--lane-cpu-cores totals {sum(counts)} physical cores but {len(core_groups)} are available"
        )
    out: list[list[int]] = [[] for _ in counts]
    assigned = [0] * len(counts)
    credit = [0] * len(counts)
    total = sum(counts)
    for group in core_groups:
        for i, weight in enumerate(counts):
            if assigned[i] < weight:
                credit[i] += weight
        candidates = [i for i in range(len(counts)) if assigned[i] < counts[i]]
        lane = max(candidates, key=lambda i: (credit[i], -i))
        credit[lane] -= total
        assigned[lane] += 1
        out[lane].extend(group)
    return [tuple(sorted(x)) for x in out]


def _set_process_affinity(cpus: tuple[int, ...]) -> None:
    os.sched_setaffinity(0, set(cpus))


def default_state_dir(config: Path, gpus: list[str]) -> Path:
    key = hashlib.sha256(
        (str(config.resolve()) + "\0" + ",".join(gpus)).encode("utf-8")
    ).hexdigest()[:16]
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "strata" / "multigpu" / key


def default_arena_file(pack: Path, spec: ArenaSpec) -> Path:
    key = hashlib.sha256(
        (str(pack.resolve()) + f"\0{spec.expert_bytes}\0{spec.max_blob}").encode("utf-8")
    ).hexdigest()[:16]
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "strata" / "shared-arena" / f"{pack.name}-{key}.bin"


@dataclass
class Lane:
    index: int
    gpu: str
    port: int
    context: int
    config: Path
    cpus: tuple[int, ...] | None = None
    pcie_frac: float | None = None
    kv_resident: int | None = None
    vision: bool = False
    process: subprocess.Popen | None = None
    busy: bool = False


class LanePool:
    def __init__(self, lanes: list[Lane]):
        self.lanes = lanes
        self.cv = threading.Condition()
        self.cursor = 0
        self.vision_waiters = 0

    def acquire(self, *, requires_vision: bool = False) -> Lane:
        with self.cv:
            if requires_vision:
                self.vision_waiters += 1
            try:
                while True:
                    alive = [x for x in self.lanes if x.process is not None and x.process.poll() is None]
                    if not alive:
                        raise RuntimeError("all GPU lanes have stopped")
                    eligible = [x for x in alive if not requires_vision or x.vision]
                    if not eligible:
                        raise RuntimeError("no vision-capable GPU lanes are running")
                    for offset in range(len(self.lanes)):
                        idx = (self.cursor + offset) % len(self.lanes)
                        lane = self.lanes[idx]
                        if lane not in eligible or lane.busy:
                            continue
                        if not requires_vision and lane.vision and self.vision_waiters:
                            continue
                        lane.busy = True
                        self.cursor = (idx + 1) % len(self.lanes)
                        return lane
                    self.cv.wait(timeout=1.0)
            finally:
                if requires_vision:
                    self.vision_waiters -= 1

    def release(self, lane: Lane) -> None:
        with self.cv:
            lane.busy = False
            self.cv.notify_all()

    def status(self) -> list[dict]:
        return [
            {
                "index": x.index,
                "gpu": x.gpu,
                "port": x.port,
                "context": x.context,
                "cpus": list(x.cpus) if x.cpus else None,
                "pcie_frac": x.pcie_frac,
                "kv_resident": x.kv_resident,
                "vision": x.vision,
                "pid": x.process.pid if x.process else None,
                "alive": bool(x.process and x.process.poll() is None),
                "busy": x.busy,
            }
            for x in self.lanes
        ]


def wait_ready(lane: Lane, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{lane.port}/health"
    while time.monotonic() < deadline:
        if lane.process is not None and lane.process.poll() is not None:
            raise RuntimeError(
                f"lane {lane.index} (GPU {lane.gpu}) exited with code {lane.process.returncode}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if 200 <= r.status < 300:
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"lane {lane.index} (GPU {lane.gpu}) did not become ready")


def stop_lane(lane: Lane) -> None:
    p = lane.process
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=5)


def make_handler(pool: LanePool, lane0: Lane, arena_file: Path, arena_bytes: int, kv_budget: int | None,
                 reject_generate_proxy: bool = False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print("[strata-multigpu] " + (fmt % args), flush=True)

        def _json(self, status: int, value) -> None:
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _status(self):
            self._json(200, {
                "status": "ok",
                "mode": "partitioned-multigpu-v1",
                "arena_file": str(arena_file),
                "arena_bytes": arena_bytes,
                "kv_budget": kv_budget,
                "lane_context_total": sum(x.context for x in pool.lanes),
                "lanes": pool.status(),
            })

        def _slots(self):
            self._json(200, [
                {"id": lane.index, "n_ctx": lane.context,
                 "is_processing": lane.busy, "gpu": lane.gpu, "vision": lane.vision}
                for lane in pool.lanes if lane.process is not None and lane.process.poll() is None
            ])

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                raise ValueError("chunked request bodies are not supported by the multigpu proxy")
            n = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(n) if n else b""

        def _proxy(self, lane: Lane, body: bytes | None = None):
            if body is None:
                try:
                    body = self._body()
                except ValueError as e:
                    return self._json(411, {"error": {"message": str(e)}})

            headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in HOP_BY_HOP and k.lower() != "host"
            }
            conn = http.client.HTTPConnection("127.0.0.1", lane.port, timeout=None)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status, resp.reason)
                has_length = False
                for k, v in resp.getheaders():
                    lk = k.lower()
                    if lk in HOP_BY_HOP:
                        continue
                    if lk == "content-length":
                        has_length = True
                    self.send_header(k, v)
                if not has_length:
                    self.send_header("connection", "close")
                    self.close_connection = True
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        chunk = resp.read1(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:
                if not self.wfile.closed:
                    self.close_connection = True
                    print(f"[strata-multigpu] lane {lane.index} proxy error: {e}", flush=True)
            finally:
                conn.close()

        def _dispatch(self):
            path = self.path.split("?", 1)[0]
            if path == "/__multigpu/status":
                return self._status()
            if path == "/slots" and self.command in ("GET", "HEAD"):
                return self._slots()
            if reject_generate_proxy and path in GENERATE_PATHS and self.command == "POST":
                return self._json(503, {"error": {"message": "benchmark isolation: public generation disabled"}})
            leased = path in GENERATE_PATHS and self.command == "POST"
            body = None
            requires_vision = False
            if leased:
                try:
                    body = self._body()
                except ValueError as e:
                    return self._json(411, {"error": {"message": str(e)}})
                requires_vision = request_has_images(body)
            try:
                lane = pool.acquire(requires_vision=requires_vision) if leased else lane0
            except RuntimeError as e:
                return self._json(503, {"error": {"message": str(e)}})
            try:
                if lane.process is None or lane.process.poll() is not None:
                    return self._json(503, {"error": {"message": f"GPU lane {lane.index} is not running"}})
                self._proxy(lane, body=body)
            finally:
                if leased:
                    pool.release(lane)

        do_GET = _dispatch
        do_HEAD = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_PATCH = _dispatch
        do_DELETE = _dispatch
        do_OPTIONS = _dispatch

    return Handler


def main() -> int:
    def _shutdown_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _shutdown_signal)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="existing Strata JSON config written by setup.py")
    ap.add_argument("--gpus", default="0,1,2", help="physical GPU ids, default: 0,1,2")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18086, help="public proxy port")
    ap.add_argument("--base-port", type=int, default=19086, help="first private lane port")
    ap.add_argument("--lane-contexts", help="fixed per-lane capacities, e.g. 262144,131072,131072")
    ap.add_argument("--kv-budget", type=int, help="total host-KV token capacity guard, e.g. 524288")
    ap.add_argument("--allow-context-over-262k", action="store_true",
                    help="allow a lane above the model's currently validated 262144-token context")
    ap.add_argument("--arena-file", help="shared expert arena backing file (Linux only)")
    ap.add_argument("--state-dir", help="directory for generated lane configs")
    ap.add_argument("--startup-timeout", type=float, default=900.0)
    ap.add_argument("--cpu-partition", choices=("none", "auto"), default="auto",
                    help="partition physical CPU cores between lanes before Strata starts (default: auto)")
    ap.add_argument("--lane-cpu-cores",
                    help="exact physical-core counts per lane, e.g. 5,6,5; overrides --cpu-partition auto")
    ap.add_argument("--lane-pcie-fracs",
                    help="per-lane --pcie-frac values, e.g. 0.55,0.30,0.55")
    ap.add_argument("--lane-kv-residents",
                    help="per-lane --kv-resident token counts, e.g. 65536,32768,32768")
    ap.add_argument("--vision-lanes",
                    help="0-based lane indices allowed to serve images, e.g. 1 or 0,2; defaults to lane 0 when vision is configured")
    ap.add_argument("--private-arena", action="store_true",
                    help="benchmark only: use the normal private expert arena in each lane")
    ap.add_argument("--reject-generate-proxy", action="store_true",
                    help="benchmark only: reject generation on the supervisor proxy; private lanes still work")
    a = ap.parse_args()

    if os.name == "nt":
        ap.error("the shared expert arena v1 is Linux-only")
    config = Path(a.config).expanduser().resolve()
    cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg.get("args"), list):
        ap.error("config has no args list")

    gpus = [x.strip() for x in a.gpus.split(",") if x.strip()]
    if not gpus or len(set(gpus)) != len(gpus):
        ap.error("--gpus must contain unique GPU ids")
    try:
        if a.vision_lanes is not None:
            vision_lanes = parse_lane_indices(a.vision_lanes, len(gpus), what="--vision-lanes")
            if vision_lanes and not cfg.get("vision"):
                raise ValueError("--vision-lanes requires a base config with a vision entry")
        else:
            vision_lanes = {0} if cfg.get("vision") else set()
    except ValueError as e:
        ap.error(str(e))
    base_ctx_raw = option_value(cfg["args"], "--max-context")
    if base_ctx_raw is None:
        ap.error("base config has no --max-context")
    try:
        base_context = int(base_ctx_raw)
        contexts = lane_contexts(base_context, len(gpus), a.lane_contexts, a.kv_budget)
    except ValueError as e:
        ap.error(str(e))
    if not a.allow_context_over_262k and any(x > 262144 for x in contexts):
        ap.error("a lane exceeds 262144 tokens; use smaller lane capacities or --allow-context-over-262k")

    pack_raw = option_value(cfg["args"], "--pack")
    if not pack_raw:
        ap.error("base config has no --pack")
    pack = resolve_config_path(pack_raw, cfg.get("cwd"))
    try:
        spec = native_arena_spec(pack)
    except (OSError, ValueError) as e:
        ap.error(f"cannot derive the native expert arena from {pack}: {e}")

    state_dir = Path(a.state_dir).expanduser().resolve() if a.state_dir else default_state_dir(config, gpus)
    state_dir.mkdir(parents=True, exist_ok=True)
    arena_file = Path(a.arena_file).expanduser().resolve() if a.arena_file else default_arena_file(pack, spec)
    arena_file.parent.mkdir(parents=True, exist_ok=True)

    core_groups = physical_core_cpu_sets()
    cpu_sets: list[tuple[int, ...] | None] = [None] * len(gpus)
    try:
        if a.lane_cpu_cores:
            cpu_counts = parse_int_list(a.lane_cpu_cores, what="--lane-cpu-cores")
            if len(cpu_counts) != len(gpus):
                raise ValueError(f"--lane-cpu-cores has {len(cpu_counts)} values for {len(gpus)} GPU lanes")
            cpu_sets = list(partition_cpu_sets_exact(core_groups, cpu_counts))
        elif a.cpu_partition == "auto":
            cpu_sets = list(partition_cpu_sets(core_groups, len(gpus)))
    except ValueError as e:
        ap.error(str(e))

    pcie_fracs: list[float | None] = [None] * len(gpus)
    if a.lane_pcie_fracs:
        try:
            values = parse_float_list(a.lane_pcie_fracs, what="--lane-pcie-fracs")
            if len(values) != len(gpus):
                raise ValueError(f"--lane-pcie-fracs has {len(values)} values for {len(gpus)} GPU lanes")
            pcie_fracs = list(values)
        except ValueError as e:
            ap.error(str(e))

    kv_residents: list[int | None] = [None] * len(gpus)
    if a.lane_kv_residents:
        try:
            values = parse_int_list(a.lane_kv_residents, what="--lane-kv-residents")
            if len(values) != len(gpus):
                raise ValueError(f"--lane-kv-residents has {len(values)} values for {len(gpus)} GPU lanes")
            if any(v > ctx for v, ctx in zip(values, contexts)):
                raise ValueError("--lane-kv-residents cannot exceed the corresponding lane context")
            kv_residents = list(values)
        except ValueError as e:
            ap.error(str(e))

    lanes: list[Lane] = []
    for i, (gpu, ctx) in enumerate(zip(gpus, contexts)):
        lane_cfg = sanitize_lane_config(cfg)
        lane_vision = i in vision_lanes
        lane_cfg = apply_vision_capability(lane_cfg, lane_vision)
        lane_cfg["args"] = replace_option(lane_cfg["args"], "--max-context", ctx)
        if pcie_fracs[i] is not None:
            lane_cfg["args"] = replace_option(lane_cfg["args"], "--pcie-frac", pcie_fracs[i])
        if kv_residents[i] is not None:
            lane_cfg["args"] = replace_option(lane_cfg["args"], "--kv-resident", kv_residents[i])
        lane_cfg["host"] = "127.0.0.1"
        if cfg.get("log"):
            log = resolve_config_path(cfg["log"], cfg.get("cwd"))
            lane_cfg["log"] = str(log.with_name(f"{log.stem}.gpu{gpu}{log.suffix or '.log'}"))
        else:
            lane_cfg["log"] = str(state_dir / f"lane-{i}-gpu{gpu}.log")
        lane_config = state_dir / f"lane-{i}-gpu{gpu}.json"
        lane_config.write_text(json.dumps(lane_cfg, indent=1), encoding="utf-8")
        lanes.append(Lane(i, gpu, a.base_port + i, ctx, lane_config, cpu_sets[i], pcie_fracs[i], kv_residents[i], lane_vision))

    print(
        f"[strata-multigpu] {len(lanes)} lanes; context capacities {contexts} "
        f"(total {sum(contexts):,}); shared expert arena {spec.expert_bytes / 2**30:.2f} GiB",
        flush=True,
    )
    print(f"[strata-multigpu] arena backing: {arena_file} ({spec.bytes:,} bytes)", flush=True)
    if vision_lanes:
        print(
            "[strata-multigpu] vision lanes: " +
            ", ".join(f"lane {x.index}=GPU{x.gpu}" for x in lanes if x.vision),
            flush=True,
        )
    if any(x.cpus for x in lanes):
        print(
            "[strata-multigpu] CPU partitions: " +
            "; ".join(f"lane {x.index}={','.join(map(str, x.cpus or ())) }" for x in lanes),
            flush=True,
        )
    if any(x.pcie_frac is not None for x in lanes):
        print(
            "[strata-multigpu] PCIe fractions: " +
            ", ".join(f"lane {x.index}={x.pcie_frac:.3f}" for x in lanes if x.pcie_frac is not None),
            flush=True,
        )
    if any(x.kv_resident is not None for x in lanes):
        print(
            "[strata-multigpu] KV resident: " +
            ", ".join(f"lane {x.index}={x.kv_resident}" for x in lanes if x.kv_resident is not None),
            flush=True,
        )

    started: list[Lane] = []
    try:
        for lane in lanes:
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = lane.gpu
            if a.private_arena:
                env.pop("STRATA_SHARED_ARENA_FILE", None)
                env.pop("STRATA_SHARED_ARENA_BYTES", None)
            else:
                env["STRATA_SHARED_ARENA_FILE"] = str(arena_file)
                env["STRATA_SHARED_ARENA_BYTES"] = str(spec.bytes)
            cmd = [
                sys.executable, str(ROOT / "serve" / "server.py"),
                "--engine", "strata",
                "--config", str(lane.config),
                "--host", "127.0.0.1",
                "--port", str(lane.port),
            ]
            print(
                f"[strata-multigpu] starting lane {lane.index}: GPU {lane.gpu}, "
                f"context {lane.context:,}, port {lane.port}",
                flush=True,
            )
            preexec_fn = None
            if lane.cpus and hasattr(os, "sched_setaffinity"):
                preexec_fn = lambda cpus=lane.cpus: _set_process_affinity(cpus)
            lane.process = subprocess.Popen(cmd, cwd=str(ROOT), env=env, preexec_fn=preexec_fn)
            started.append(lane)
            wait_ready(lane, a.startup_timeout)
            print(f"[strata-multigpu] lane {lane.index} ready", flush=True)

        pool = LanePool(lanes)
        httpd = ThreadingHTTPServer(
            (a.host, a.port),
            make_handler(pool, lanes[0], arena_file, spec.bytes, a.kv_budget, a.reject_generate_proxy),
        )
        print(
            f"[strata-multigpu] ready: http://{a.host}:{a.port}/v1 "
            f"({len(lanes)} concurrent generation lanes)",
            flush=True,
        )
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
    except KeyboardInterrupt:
        pass
    finally:
        for lane in reversed(started):
            stop_lane(lane)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
