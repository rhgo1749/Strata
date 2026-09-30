from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("strata_multigpu_server", HERE / "multigpu_server.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


class _AliveProcess:
    def poll(self):
        return None


class MultiGpuPlanningTests(unittest.TestCase):
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
        self.assertIn("gpu", cfg)
        self.assertIn("--layer-split", cfg["args"])

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

    def test_metadata_prefers_healthy_vision_lane(self):
        lanes = [
            M.Lane(0, "0", 19086, 262144, Path("lane0.json"), vision=False, process=_AliveProcess()),
            M.Lane(1, "1", 19087, 262144, Path("lane1.json"), vision=True, process=_AliveProcess()),
        ]
        pool = M.LanePool(lanes)
        self.assertEqual(pool.metadata_lane(lanes[0]).index, 1)

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
