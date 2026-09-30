from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("strata_multigpu_server", HERE / "multigpu_server.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


class _AliveProcess:
    pid = 12345

    def poll(self):
        return None


class MultiGpuPlanningTests(unittest.TestCase):
    def setUp(self):
        self._lane_engine_alive = M.lane_engine_alive
        M.lane_engine_alive = lambda lane: lane.process is not None and lane.process.poll() is None

    def tearDown(self):
        M.lane_engine_alive = self._lane_engine_alive

    def test_replace_existing_option(self):
        args = ["--pack", "/m", "--max-context", "131072", "--kv", "int8"]
        self.assertEqual(
            M.replace_option(args, "--max-context", 262144),
            ["--pack", "/m", "--max-context", "262144", "--kv", "int8"],
        )
        self.assertEqual(args[3], "131072")

    def test_replace_missing_option(self):
        self.assertEqual(M.replace_option(["--pack", "/m"], "--max-context", 65536),
                         ["--pack", "/m", "--max-context", "65536"])

    def test_remove_option_removes_all_layer_split_pairs(self):
        self.assertEqual(
            M.remove_option(["--pack", "/m", "--layer-split", "auto", "--kv", "int8",
                             "--layer-split", "16,32"], "--layer-split"),
            ["--pack", "/m", "--kv", "int8"],
        )

    def test_lane_config_cannot_reexpand_into_layer_split(self):
        cfg = {"gpu": [0, 1, 2], "layer_split": "16,32",
               "args": ["--pack", "/m", "--layer-split", "auto", "--max-context", "262144"]}
        got = M.sanitize_lane_config(cfg)
        self.assertNotIn("gpu", got)
        self.assertNotIn("layer_split", got)
        self.assertNotIn("--layer-split", got["args"])
        self.assertEqual(M.option_value(got["args"], "--conversation-cache-mib"), "0")
        self.assertIn("gpu", cfg)
        self.assertIn("--layer-split", cfg["args"])

    def test_lane_config_disables_upstream_conversation_parking(self):
        cfg = {"args": ["--pack", "/m", "--conversation-cache-mib", "8192",
                         "--conversation-cache-slots", "4"]}
        got = M.sanitize_lane_config(cfg)
        self.assertEqual(M.option_value(got["args"], "--conversation-cache-mib"), "0")
        self.assertEqual(M.option_value(got["args"], "--conversation-cache-slots"), "4")

    def test_nonvision_lane_drops_encoder_and_vram_reserve(self):
        cfg = {"vision": {"exe": "/v", "gpu": True},
               "args": ["--pack", "/m", "--vision", "--vram-reserve-mib", "700", "--max-context", "262144"]}
        got = M.apply_vision_capability(M.sanitize_lane_config(cfg), False)
        self.assertNotIn("vision", got)
        self.assertNotIn("--vision", got["args"])
        self.assertNotIn("--vram-reserve-mib", got["args"])
        self.assertIn("vision", cfg)

    def test_vision_lane_keeps_encoder_config(self):
        cfg = {"vision": {"exe": "/v", "gpu": True},
               "args": ["--pack", "/m", "--vision", "--vram-reserve-mib", "700"]}
        got = M.apply_vision_capability(M.sanitize_lane_config(cfg), True)
        self.assertIn("vision", got)
        self.assertIn("--vision", got["args"])
        self.assertIn("--vram-reserve-mib", got["args"])

    def test_nonvision_lane_can_reapply_explicit_vram_reserve(self):
        cfg = {"vision": {"exe": "/v", "gpu": True},
               "args": ["--pack", "/m", "--vision", "--vram-reserve-mib", "700"]}
        got = M.apply_vision_capability(M.sanitize_lane_config(cfg), False)
        self.assertNotIn("--vram-reserve-mib", got["args"])
        got["args"] = M.replace_option(got["args"], "--vram-reserve-mib", 1200)
        self.assertEqual(M.option_value(got["args"], "--vram-reserve-mib"), "1200")

    def test_shared_arena_uses_upstream_cli_and_replaces_stale_value(self):
        cfg = {"args": ["--pack", "/m", "--shared-expert-arena", "/old"]}
        got = M.apply_shared_arena(cfg, Path("/dev/shm/strata/new.bin"))
        self.assertEqual(M.option_value(got["args"], "--shared-expert-arena"), "/dev/shm/strata/new.bin")
        self.assertEqual(got["args"].count("--shared-expert-arena"), 1)
        self.assertEqual(M.option_value(cfg["args"], "--shared-expert-arena"), "/old")

    def test_private_arena_strips_inherited_shared_arena(self):
        cfg = {"args": ["--pack", "/m", "--shared-expert-arena", "/old", "--kv", "int8"]}
        got = M.apply_shared_arena(cfg, None)
        self.assertNotIn("--shared-expert-arena", got["args"])
        self.assertEqual(got["args"], ["--pack", "/m", "--kv", "int8"])

    def test_default_shared_arena_is_tmpfs(self):
        spec = M.ArenaSpec(bytes=100, expert_bytes=80, max_blob=20, n_expert=512)
        path = M.default_arena_file(Path("/models/pack"), spec)
        self.assertEqual(path.parts[:3], ("/", "dev", "shm"))

    def test_port_preflight_rejects_stale_listener(self):
        sock = M.socket.socket(M.socket.AF_INET, M.socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                M.require_ports_free("127.0.0.1", [port])
        finally:
            sock.close()

    def test_port_preflight_accepts_free_port_and_ephemeral_public_port(self):
        sock = M.socket.socket(M.socket.AF_INET, M.socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        M.require_ports_free("127.0.0.1", [port, 0])

    def test_benchmark_trace_records_exact_lane_lease_and_headers(self):
        class Backend(M.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n:
                    self.rfile.read(n)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        backend = M.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
        backend_thread.start()
        lane = M.Lane(0, "0", backend.server_address[1], 32768, Path("lane.json"), process=_AliveProcess())
        pool = M.LanePool([lane])

        with tempfile.TemporaryDirectory() as td:
            trace_path = Path(td) / "leases.jsonl"
            trace = M.BenchmarkTrace(trace_path)
            handler = M.make_handler(pool, lane, Path("/dev/shm/fake.bin"), 123, None, bench_trace=trace)
            proxy = M.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            try:
                conn = M.http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
                body = b'{"messages":[{"role":"user","content":"hi"}]}'
                conn.request(
                    "POST", "/v1/chat/completions", body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                        "X-Strata-Benchmark-Run-Id": "run-a",
                        "X-Strata-Benchmark-Request-Id": "req-3",
                        "X-Strata-Benchmark-Submit-Rank": "3",
                    },
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.getheader("X-Strata-Lane-Index"), "0")
                self.assertEqual(resp.getheader("X-Strata-Admission-Rank"), "0")
                self.assertEqual(resp.getheader("X-Strata-Benchmark-Request-Id"), "req-3")
                self.assertGreaterEqual(float(resp.getheader("X-Strata-Queue-Wait-Ms")), 0.0)
                self.assertEqual(resp.read(), b'{"ok":true}')

                conn.request(
                    "POST", "/v1/chat/completions", body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                        "X-Strata-Benchmark-Run-Id": "run-b",
                        "X-Strata-Benchmark-Request-Id": "req-b0",
                        "X-Strata-Benchmark-Submit-Rank": "0",
                    },
                )
                resp2 = conn.getresponse()
                self.assertEqual(resp2.status, 200)
                self.assertEqual(resp2.getheader("X-Strata-Admission-Rank"), "0")
                self.assertEqual(resp2.read(), b'{"ok":true}')
                conn.close()
            finally:
                proxy.shutdown()
                proxy.server_close()
                backend.shutdown()
                backend.server_close()
                proxy_thread.join(1.0)
                backend_thread.join(1.0)

            records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2)
            rec = records[0]
            self.assertEqual(rec["run_id"], "run-a")
            self.assertEqual(rec["request_id"], "req-3")
            self.assertEqual(rec["submit_rank"], "3")
            self.assertEqual(rec["admission_rank"], 0)
            self.assertEqual(rec["lane_index"], 0)
            self.assertGreaterEqual(rec["queue_wait_ms"], 0.0)
            self.assertGreaterEqual(rec["service_ms"], 0.0)
            self.assertLessEqual(rec["queue_enter_unix_ns"], rec["admitted_unix_ns"])
            self.assertLessEqual(rec["admitted_unix_ns"], rec["released_unix_ns"])
            self.assertEqual(records[1]["run_id"], "run-b")
            self.assertEqual(records[1]["admission_rank"], 0)

    def test_parse_vision_lane_indices(self):
        self.assertEqual(M.parse_lane_indices("1", 3, what="--vision-lanes"), {1})
        self.assertEqual(M.parse_lane_indices("0,2", 3, what="--vision-lanes"), {0, 2})
        self.assertEqual(M.parse_lane_indices("none", 3, what="--vision-lanes"), set())
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            M.parse_lane_indices("3", 3, what="--vision-lanes")

    def test_detect_supported_image_message_parts(self):
        openai = b'{"messages":[{"role":"user","content":[{"type":"text","text":"look"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]}]}'
        anthropic = b'{"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AA=="}}]}]}'
        text = b'{"messages":[{"role":"user","content":"hello"}]}'
        self.assertTrue(M.request_has_images(openai))
        self.assertTrue(M.request_has_images(anthropic))
        self.assertFalse(M.request_has_images(text))
        self.assertFalse(M.request_has_images(b"not-json"))

    def test_affinity_key_survives_appended_chat_history(self):
        first = {"messages": [{"role": "system", "content": "rules"},
                              {"role": "user", "content": "build the thing"}]}
        later = {"messages": first["messages"] + [{"role": "assistant", "content": "working"},
                                                   {"role": "user", "content": "continue"}]}
        self.assertEqual(
            M.request_affinity_key(json.dumps(first).encode()),
            M.request_affinity_key(json.dumps(later).encode()),
        )

    def test_explicit_affinity_id_wins_over_message_fallback(self):
        body = b'{"messages":[{"role":"user","content":"hello"}]}'
        explicit = M.request_affinity_key(body, {"x-strata-session-id": "session-a"})
        fallback = M.request_affinity_key(body)
        self.assertIsNotNone(explicit)
        self.assertNotEqual(explicit, fallback)
        self.assertEqual(explicit, M.request_affinity_key(b'{"messages":[]}', {"x-strata-session-id": "session-a"}))

    def test_vision_request_uses_only_vision_lane(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), vision=False, process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), vision=True, process=_AliveProcess()),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), vision=False, process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        got = pool.acquire(requires_vision=True)
        self.assertEqual(got.index, 1)
        pool.release(got)

    def test_text_request_yields_vision_lane_to_waiting_image(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), vision=False, process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), vision=True, process=_AliveProcess()),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), vision=False, process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        pool.cursor = 1
        pool.vision_waiters = 1
        got = pool.acquire()
        self.assertEqual(got.index, 2)
        pool.release(got)

    def test_affinity_waits_for_its_busy_lane_instead_of_spilling(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(), busy=True),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        pool.affinity["chat-a"] = 0
        pool.cursor = 1
        started = threading.Event()
        result = []

        def acquire():
            started.set()
            result.append(pool.acquire(affinity_key="chat-a"))

        thread = threading.Thread(target=acquire)
        thread.start()
        self.assertTrue(started.wait(1.0))
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        pool.release(lanes[0])
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].index, 0)
        pool.release(result[0])

    def test_affinity_waiter_reserves_released_lane_from_new_session(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(), busy=True),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(), busy=True),
        ]
        pool = M.LanePool(lanes)
        pool.affinity["returning"] = 0
        returning = []
        newcomer = []

        t_returning = threading.Thread(target=lambda: returning.append(pool.acquire(affinity_key="returning")))
        t_returning.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with pool.cv:
                if pool.affinity_waiters[0] == 1:
                    break
            time.sleep(0.005)
        self.assertEqual(pool.affinity_waiters[0], 1)

        t_new = threading.Thread(target=lambda: newcomer.append(pool.acquire(affinity_key="new-chat")))
        t_new.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with pool.cv:
                if len(pool.waiters) == 1:
                    break
            time.sleep(0.005)
        self.assertEqual(len(pool.waiters), 1)

        pool.release(lanes[0], affinity_key="old", request_bytes=1000)
        t_returning.join(1.0)
        self.assertFalse(t_returning.is_alive())
        self.assertEqual(returning[0].index, 0)
        time.sleep(0.05)
        self.assertTrue(t_new.is_alive())

        pool.release(lanes[1], affinity_key="other", request_bytes=1000)
        t_new.join(1.0)
        self.assertFalse(t_new.is_alive())
        self.assertEqual(newcomer[0].index, 1)
        pool.release(returning[0], affinity_key="returning", request_bytes=1100)
        pool.release(newcomer[0], affinity_key="new-chat", request_bytes=500)

    def test_four_waiting_new_sessions_use_compatible_fifo_order(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(), busy=True),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(), busy=True),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess(), busy=True),
        ]
        pool = M.LanePool(lanes)
        results = []
        threads = []

        for i in range(4):
            thread = threading.Thread(
                target=lambda i=i: results.append((i, pool.acquire(affinity_key=f"q{i}").index))
            )
            thread.start()
            threads.append(thread)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with pool.cv:
                    if len(pool.waiters) == i + 1:
                        break
                time.sleep(0.005)
            self.assertEqual(len(pool.waiters), i + 1)

        for expected, lane_index in ((0, 2), (1, 0), (2, 1)):
            pool.release(lanes[lane_index], affinity_key=f"initial-{lane_index}", request_bytes=1000)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and len(results) <= expected:
                time.sleep(0.005)
            self.assertEqual(results[expected], (expected, lane_index))

        self.assertTrue(threads[3].is_alive())
        pool.release(lanes[2], affinity_key="q0", request_bytes=500)
        threads[3].join(1.0)
        self.assertFalse(threads[3].is_alive())
        self.assertEqual(results[3], (3, 2))
        for thread in threads[:3]:
            thread.join(1.0)
        for lane in lanes:
            if lane.busy:
                pool.release(lane)

    def test_new_session_waits_when_all_lanes_busy(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(), busy=True,
                   live_affinity_key="a", live_request_bytes=200000),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(), busy=True,
                   live_affinity_key="b", live_request_bytes=60000),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess(), busy=True,
                   live_affinity_key="c", live_request_bytes=700),
        ]
        pool = M.LanePool(lanes)
        started = threading.Event()
        result = []

        def acquire():
            started.set()
            result.append(pool.acquire(affinity_key="new-chat", request_bytes=500))

        thread = threading.Thread(target=acquire)
        thread.start()
        self.assertTrue(started.wait(1.0))
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        pool.release(lanes[1], affinity_key="b", request_bytes=60000)
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].index, 1)
        pool.release(result[0], affinity_key="new-chat", request_bytes=500)

    def test_unidentified_live_state_is_not_treated_as_empty(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(),
                   live_affinity_key=None, live_request_bytes=5000, live_sequence=1),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(),
                   live_affinity_key="small", live_request_bytes=1000, live_sequence=2),
        ]
        pool = M.LanePool(lanes)
        got = pool.acquire(affinity_key="new-chat", request_bytes=500)
        self.assertEqual(got.index, 1)
        pool.release(got, affinity_key="new-chat", request_bytes=500)

    def test_new_session_prefers_empty_live_lane(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(),
                   live_affinity_key="long", live_request_bytes=200000),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess()),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess(),
                   live_affinity_key="medium", live_request_bytes=60000),
        ]
        pool = M.LanePool(lanes)
        got = pool.acquire(affinity_key="new-chat", request_bytes=500)
        self.assertEqual(got.index, 1)
        pool.release(got, affinity_key="new-chat", request_bytes=500)

    def test_new_session_prefers_smallest_live_state_without_lane_hardcoding(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(),
                   live_affinity_key="long", live_request_bytes=200000),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(),
                   live_affinity_key="medium", live_request_bytes=60000),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess(),
                   live_affinity_key="short", live_request_bytes=700),
        ]
        pool = M.LanePool(lanes)
        got = pool.acquire(affinity_key="new-chat", request_bytes=500)
        self.assertEqual(got.index, 2)
        pool.release(got, affinity_key="new-chat", request_bytes=500)

    def test_equal_size_prefers_least_recent_live_state(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(),
                   live_affinity_key="a", live_request_bytes=1000, live_sequence=30),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(),
                   live_affinity_key="b", live_request_bytes=1000, live_sequence=10),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess(),
                   live_affinity_key="c", live_request_bytes=1000, live_sequence=20),
        ]
        pool = M.LanePool(lanes)
        got = pool.acquire(affinity_key="new-chat", request_bytes=500)
        self.assertEqual(got.index, 1)
        pool.release(got, affinity_key="new-chat", request_bytes=500)

    def test_equal_live_states_use_rotating_candidate_order(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess(),
                   live_affinity_key="a", live_request_bytes=1000),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess(),
                   live_affinity_key="b", live_request_bytes=1000),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess(),
                   live_affinity_key="c", live_request_bytes=1000),
        ]
        pool = M.LanePool(lanes)
        pool.cursor = 1
        got = pool.acquire(affinity_key="new-chat", request_bytes=500)
        self.assertEqual(got.index, 1)
        pool.release(got, affinity_key="new-chat", request_bytes=500)

    def test_other_session_does_not_erase_existing_lane_affinity(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess()),
            M.Lane(2, "2", 19088, 262144, Path("lane2.json"), process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        first = pool.acquire(affinity_key="long-chat", request_bytes=200000)
        self.assertEqual(first.index, 0)
        pool.release(first, affinity_key="long-chat", request_bytes=200000)
        for key, size in (("chat-b", 60000), ("chat-c", 700)):
            lane = pool.acquire(affinity_key=key, request_bytes=size)
            pool.release(lane, affinity_key=key, request_bytes=size)
        lane = pool.acquire(affinity_key="short-chat", request_bytes=500)
        self.assertEqual(lane.index, 2)
        pool.release(lane, affinity_key="short-chat", request_bytes=500)
        self.assertEqual(pool.affinity["long-chat"], 0)
        again = pool.acquire(affinity_key="long-chat", request_bytes=201000)
        self.assertEqual(again.index, 0)
        pool.release(again, affinity_key="long-chat", request_bytes=201000)

    def test_affinity_lru_is_bounded(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes, max_affinity_entries=3)
        for key in ("a", "b", "c", "d"):
            lane = pool.acquire(affinity_key=key)
            pool.release(lane)
        self.assertNotIn("a", pool.affinity)
        self.assertEqual(set(pool.affinity), {"b", "c", "d"})

    def test_vision_request_rebinds_affinity_to_a_vision_lane(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), vision=False, process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), vision=True, process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        pool.affinity["chat-a"] = 0
        got = pool.acquire(requires_vision=True, affinity_key="chat-a")
        self.assertEqual(got.index, 1)
        self.assertEqual(pool.affinity["chat-a"], 1)
        pool.release(got)

    def test_metadata_prefers_healthy_vision_lane(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), vision=False, process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), vision=True, process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        old = M.lane_engine_alive
        M.lane_engine_alive = lambda lane: lane.index == 1
        try:
            self.assertEqual(pool.metadata_lane(lanes[0]).index, 1)
        finally:
            M.lane_engine_alive = old
        self.assertIn("/health", M.VISION_METADATA_PATHS)

    def test_status_distinguishes_wrapper_from_child_engine_health(self):
        lane = M.Lane(0, "0", 19086, 262144, Path("lane0.json"), vram_reserve_mib=1200,
                      process=_AliveProcess())
        pool = M.LanePool([lane])
        old = M.lane_engine_alive
        M.lane_engine_alive = lambda _: False
        try:
            status = pool.status()[0]
        finally:
            M.lane_engine_alive = old
        self.assertTrue(status["wrapper_alive"])
        self.assertFalse(status["alive"])
        self.assertEqual(status["vram_reserve_mib"], 1200)

    def test_acquire_excludes_wrapper_alive_child_dead_lane(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        old = M.lane_engine_alive
        M.lane_engine_alive = lambda lane: lane.index == 1
        try:
            got = pool.acquire(affinity_key="new-chat")
        finally:
            M.lane_engine_alive = old
        self.assertEqual(got.index, 1)
        pool.release(got, affinity_key="new-chat")

    def test_native_arena_size_matches_arena_expert_source_contract(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "native_experts.txt").write_text(
                "# strata native experts v3 (n_expert 512, total 153600)\n"
                "0 42 2 0 100 10 20 30\n"
                "1 42 2 51200 200 40 50 60\n",
                encoding="utf-8",
            )
            got = M.native_arena_spec(p)
            self.assertEqual(got.n_expert, 512)
            self.assertEqual(got.max_blob, 200)
            self.assertEqual(got.expert_bytes, 51200 + 200 * 512)
            self.assertEqual(got.bytes, got.expert_bytes + got.max_blob)

    def test_512k_partition_for_three_lanes(self):
        got = M.lane_contexts(131072, 3, "262144,131072,131072", 524288)
        self.assertEqual(got, [262144, 131072, 131072])
        self.assertEqual(sum(got), 524288)

    def test_even_partition_when_only_budget_is_given(self):
        got = M.lane_contexts(131072, 3, None, 524288)
        self.assertEqual(sum(got), 524288)
        self.assertLessEqual(max(got) - min(got), 1)

    def test_reject_contexts_over_budget(self):
        with self.assertRaisesRegex(ValueError, "above --kv-budget"):
            M.lane_contexts(131072, 3, "262144,262144,131072", 524288)

    def test_reject_lane_count_mismatch(self):
        with self.assertRaisesRegex(ValueError, "3 GPU lanes"):
            M.lane_contexts(131072, 3, "262144,131072", 524288)

    def test_cpu_partition_keeps_smt_siblings_together(self):
        groups = [(i, i + 16) for i in range(16)]
        got = M.partition_cpu_sets(groups, 3)
        self.assertEqual([len(x) for x in got], [12, 10, 10])
        self.assertEqual(set().union(*(set(x) for x in got)), set(range(32)))
        self.assertTrue(set(got[0]).isdisjoint(got[1]))
        self.assertTrue(set(got[0]).isdisjoint(got[2]))
        self.assertTrue(set(got[1]).isdisjoint(got[2]))
        for i in range(16):
            owners = [n for n, cpus in enumerate(got) if i in cpus or i + 16 in cpus]
            self.assertEqual(len(owners), 1)
            self.assertIn(i, got[owners[0]])
            self.assertIn(i + 16, got[owners[0]])

    def test_cpu_partition_needs_host_and_worker_core_per_lane(self):
        with self.assertRaisesRegex(ValueError, "two physical cores per lane"):
            M.partition_cpu_sets([(0,), (1,), (2,), (3,), (4,)], 3)

    def test_exact_cpu_partition_biases_middle_lane(self):
        groups = [(i, i + 16) for i in range(16)]
        got = M.partition_cpu_sets_exact(groups, [5, 6, 5])
        self.assertEqual([len(x) for x in got], [10, 12, 10])
        self.assertEqual(set().union(*(set(x) for x in got)), set(range(32)))
        for i in range(16):
            owners = [n for n, cpus in enumerate(got) if i in cpus or i + 16 in cpus]
            self.assertEqual(len(owners), 1)
            self.assertIn(i, got[owners[0]])
            self.assertIn(i + 16, got[owners[0]])

    def test_exact_cpu_partition_requires_full_budget(self):
        with self.assertRaisesRegex(ValueError, "totals 15 physical cores"):
            M.partition_cpu_sets_exact([(i,) for i in range(16)], [5, 5, 5])

    def test_parse_lane_pcie_fractions(self):
        self.assertEqual(M.parse_float_list("0.55,0.30,0.75", what="--lane-pcie-fracs"), [0.55, 0.30, 0.75])
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            M.parse_float_list("0.55,1.2,0.55", what="--lane-pcie-fracs")


if __name__ == "__main__":
    unittest.main()
