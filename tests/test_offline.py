"""Offline unit tests (no network, no GPU). Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import io
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest import mock  # noqa: E402

from hub import discovery, estimates, gguf, hardware, hf, net, sources  # noqa: E402

# Keep tests independent of the machine they run on: fixed GPU 300 GB/s, CPU RAM 50 GB/s.
_patches = [mock.patch.object(hardware, "static_info", lambda force=False: {}),
            mock.patch.object(hardware, "effective_bandwidth", lambda info: {"gbps": 300, "source": "test"}),
            mock.patch.object(hardware, "cpu_bandwidth", lambda info: 50)]


def setUpModule():
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()

# Budgets as hardware.budget() returns them (GiB): a unified-memory APU (integrated, 64 + 43.6 shared),
# a discrete 24 GB card with 64 GB RAM, and a CPU-only box.
GPU = {"kind": "integrated", "dedicated_gb": 64, "reserve_gb": 2, "gpu_capacity_gb": 62 + 43.6,
       "offload_capacity_gb": 105.6, "free_now_gb": 100}
DGPU = {"kind": "discrete", "dedicated_gb": 24, "reserve_gb": 2, "gpu_capacity_gb": 22,
        "offload_capacity_gb": 22 + 44, "free_now_gb": 22}
CPU = {"kind": "none", "dedicated_gb": 0, "reserve_gb": 2, "gpu_capacity_gb": 0,
       "offload_capacity_gb": 50, "free_now_gb": 40}


def _gguf_bytes(arch: str, kv: dict) -> bytes:
    """Build a minimal GGUF header with the given key/values (uint32, string or uint32 arrays)."""
    out = io.BytesIO()
    out.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kv) + 1))

    def s(x: str) -> None:
        b = x.encode()
        out.write(struct.pack("<Q", len(b)) + b)
    s("general.architecture"); out.write(struct.pack("<I", 8)); s(arch)
    for k, v in kv.items():
        s(k)
        if isinstance(v, str):
            out.write(struct.pack("<I", 8)); s(v)
        elif isinstance(v, list):
            out.write(struct.pack("<I", 9) + struct.pack("<I", 4) + struct.pack("<Q", len(v)))
            for x in v:
                out.write(struct.pack("<I", x))
        else:
            out.write(struct.pack("<I", 4) + struct.pack("<I", v))
    return out.getvalue()


class TestGGUF(unittest.TestCase):
    def test_reads_header_and_maps_config(self):
        data = _gguf_bytes("llama", {"llama.block_count": 32, "llama.attention.head_count": 32,
                                     "llama.attention.head_count_kv": 8, "llama.embedding_length": 4096})
        cfg = gguf.to_config(gguf.read_metadata(io.BytesIO(data)))
        self.assertEqual(cfg["num_hidden_layers"], 32)
        self.assertEqual(cfg["num_key_value_heads"], 8)
        self.assertEqual(cfg["head_dim"], 128)  # 4096 / 32
        self.assertNotIn("layer_types", cfg)

    def test_hybrid_per_layer_kv(self):
        data = _gguf_bytes("nemotron_h", {"nemotron_h.block_count": 4, "nemotron_h.attention.head_count": 8,
                                          "nemotron_h.attention.head_count_kv": [0, 2, 0, 0],
                                          "nemotron_h.attention.key_length": 128})
        cfg = gguf.to_config(gguf.read_metadata(io.BytesIO(data)))
        self.assertEqual(cfg["layer_types"].count("full_attention"), 1)

    def test_gemma3_sliding_pattern(self):
        data = _gguf_bytes("gemma3", {"gemma3.block_count": 12, "gemma3.attention.head_count": 8,
                                      "gemma3.attention.head_count_kv": 4, "gemma3.attention.key_length": 128,
                                      "gemma3.attention.sliding_window": 1024})
        cfg = gguf.to_config(gguf.read_metadata(io.BytesIO(data)))
        self.assertEqual(cfg["layer_types"].count("full_attention"), 2)  # every 6th layer
        self.assertEqual(cfg["sliding_window"], 1024)

    def test_rejects_non_gguf(self):
        with self.assertRaises(gguf.GGUFError):
            gguf.read_metadata(io.BytesIO(b"PK\x03\x04 not a model"))


class TestEstimates(unittest.TestCase):
    def test_active_params_from_name(self):
        val, how = estimates.active_params(35e9, "Qwen3.6-35B-A3B", None)
        self.assertAlmostEqual(val, 3e9)
        self.assertIn("name", how)

    def test_dense_fallback(self):
        val, how = estimates.active_params(27e9, "gemma-3-27b", {})
        self.assertEqual(val, 27e9)

    def test_kv_full_attention(self):
        cfg = {"num_hidden_layers": 32, "num_attention_heads": 32, "num_key_value_heads": 8, "hidden_size": 4096}
        per_tok, _, _ = estimates.kv_bytes_per_token(cfg)
        self.assertEqual(per_tok, 32 * 2 * 8 * 128 * 2)

    def test_kv_mla(self):
        per_tok, _, how = estimates.kv_bytes_per_token({"num_hidden_layers": 60, "kv_lora_rank": 512, "qk_rope_head_dim": 64})
        self.assertEqual(per_tok, 60 * 576 * 2)
        self.assertIn("MLA", how)

    def test_fit_categories(self):
        small = estimates.estimate(name="x", weights_bytes=10e9, total_params=20e9, cfg=None, gpu=GPU)
        big = estimates.estimate(name="x", weights_bytes=90e9, total_params=120e9, cfg=None, gpu=GPU)
        huge = estimates.estimate(name="x", weights_bytes=200e9, total_params=600e9, cfg=None, gpu=GPU)
        self.assertTrue(small["fit"].startswith("fits in dedicated"))
        self.assertTrue(big["fit"].startswith("fits using shared") or big["fit"] == "does not fit")
        self.assertEqual(huge["fit"], "does not fit")

    def test_discrete_gpu_offload(self):
        fits = estimates.estimate(name="x", weights_bytes=15e9, total_params=20e9, cfg=None, gpu=DGPU)
        off = estimates.estimate(name="x", weights_bytes=40e9, total_params=70e9, cfg=None, gpu=DGPU)
        self.assertTrue(fits["fit"].startswith("fits in dedicated"))
        self.assertEqual(off["fit"], "partial CPU offload (slow)")
        full_gpu = estimates.estimate(name="x", weights_bytes=40e9, total_params=70e9, cfg=None, gpu=GPU)
        self.assertLess(off["decode_tokens_per_s"], full_gpu["decode_tokens_per_s"])

    def test_cpu_only(self):
        e = estimates.estimate(name="x", weights_bytes=8e9, total_params=14e9, cfg=None, gpu=CPU)
        self.assertEqual(e["fit"], "CPU only (slow)")

    def test_speed_scales_with_active_params(self):
        moe = estimates.estimate(name="m-30B-A3B", weights_bytes=18e9, total_params=30e9, cfg=None, gpu=GPU)
        dense = estimates.estimate(name="d-30B", weights_bytes=18e9, total_params=30e9, cfg=None, gpu=GPU)
        self.assertGreater(moe["decode_tokens_per_s"], dense["decode_tokens_per_s"] * 5)

    def test_turn_time_uses_overrides(self):
        a = estimates.estimate(name="m-A3B", weights_bytes=18e9, total_params=30e9, cfg=None, gpu=GPU, prompt_tokens=1000, output_tokens=100)
        b = estimates.estimate(name="m-A3B", weights_bytes=18e9, total_params=30e9, cfg=None, gpu=GPU, prompt_tokens=10000, output_tokens=1000)
        self.assertGreater(b["agent_turn"]["seconds"], a["agent_turn"]["seconds"])


class TestGrouping(unittest.TestCase):
    def test_split_parts_and_extras(self):
        files = [
            {"path": "Q4/M-Q4_K_M-00001-of-00002.gguf", "size": 10, "sha256": "a" * 64},
            {"path": "Q4/M-Q4_K_M-00002-of-00002.gguf", "size": 20, "sha256": "b" * 64},
            {"path": "M-Q8_0.gguf", "size": 50, "sha256": "c" * 64},
            {"path": "mmproj-M-f16.gguf", "size": 5, "sha256": "d" * 64},
            {"path": "M-imatrix.gguf", "size": 1, "sha256": "e" * 64},
            {"path": "README.md", "size": 1, "sha256": None},
        ]
        groups = hf.group_gguf(files)
        real = [g for g in groups if not g.get("extras")]
        self.assertEqual([g["quant"] for g in real], ["Q4_K_M", "Q8_0"])
        self.assertEqual(real[0]["parts"], 2)
        self.assertEqual(real[0]["size"], 30)
        extras = [g for g in groups if g.get("extras")][0]
        self.assertEqual(sorted(f["kind"] for f in extras["files"]), ["imatrix", "vision projector"])


class TestRecommend(unittest.TestCase):
    def _v(self, size_gb, bpw, fit):
        return {"variant": f"{size_gb}", "size": size_gb * 1e9,
                "estimate": {"fit": fit, "bits_per_weight": bpw}}

    def test_prefers_dedicated_sweet_spot(self):
        vs = [self._v(20, 4.5, "fits in dedicated GPU memory"), self._v(30, 6.5, "fits in dedicated GPU memory"),
              self._v(60, 16, "fits using shared memory")]
        self.assertEqual(discovery.recommend(vs, GPU)["variant"], "30")

    def test_falls_back_to_shared_near_4_6_bits(self):
        s = "fits using shared memory"
        vs = [self._v(60, 4.0, s), self._v(70, 4.7, s), self._v(85, 6.0, s)]
        self.assertEqual(discovery.recommend(vs, GPU)["variant"], "70")

    def test_discrete_falls_back_to_offload(self):
        vs = [self._v(40, 4.6, "partial CPU offload (slow)"), self._v(90, 4.6, "does not fit")]
        self.assertEqual(discovery.recommend(vs, DGPU)["variant"], "40")

    def test_low_bit_fallback_keeps_headroom(self):
        s = "fits using shared memory"
        a, b = self._v(90, 2.4, s), self._v(104, 2.7, s)
        a["estimate"]["memory_needed_gb"], b["estimate"]["memory_needed_gb"] = 95, 109
        self.assertEqual(discovery.recommend([a, b], GPU)["variant"], "90")

    def test_none_when_nothing_fits(self):
        self.assertIsNone(discovery.recommend([self._v(200, 4.5, "does not fit")], GPU))


class TestHardwareClassification(unittest.TestCase):
    """Integrated vs discrete detection across common configurations."""

    def _a(self, name, vendor, dedicated):
        return {"name": name, "vendor": vendor, "dedicated_gb": dedicated}

    def test_large_apu_carve_out(self):  # e.g. 128 GB installed, 64 GB reserved for the GPU
        from hub import dxgi
        self.assertEqual(dxgi.classify(self._a("AMD Radeon(TM) Graphics", "AMD", 64.0), 128, 63.5), "integrated")

    def test_laptop_apu_small_carve_out(self):
        from hub import dxgi
        self.assertEqual(dxgi.classify(self._a("AMD Radeon(TM) 780M Graphics", "AMD", 0.5), 32, 31.3), "integrated")

    def test_intel_igpu(self):
        from hub import dxgi
        self.assertEqual(dxgi.classify(self._a("Intel(R) UHD Graphics", "Intel", 0.125), 16, 15.8), "integrated")

    def test_discrete_nvidia(self):
        from hub import dxgi
        self.assertEqual(dxgi.classify(self._a("NVIDIA GeForce RTX 4090", "NVIDIA", 23.6), 64, 63.8), "discrete")

    def test_discrete_amd(self):
        from hub import dxgi
        self.assertEqual(dxgi.classify(self._a("AMD Radeon RX 7900 XTX", "AMD", 23.9), 64, 63.8), "discrete")

    def test_ram_bandwidth_soldered_lpddr(self):  # e.g. 8 x 32-bit LPDDR5X-8000
        bw = hardware.ram_bandwidth([{"ConfiguredClockSpeed": 8000, "DataWidth": 32, "FormFactor": 0}] * 8)
        self.assertEqual(bw["peak_gbps"], 256.0)

    def test_ram_bandwidth_desktop_dimms_dual_channel(self):  # 4 DDR5-6000 DIMMs still = 2 channels
        bw = hardware.ram_bandwidth([{"ConfiguredClockSpeed": 6000, "DataWidth": 64, "FormFactor": 8}] * 4)
        self.assertEqual(bw["peak_gbps"], 96.0)


class TestUnifiedGating(unittest.TestCase):
    """The RAM split plan only applies on unified-memory (integrated) GPUs."""

    def _with(self, kind):
        info = {"primary": {"kind": kind}}
        return mock.patch.object(hardware, "static_info", lambda force=False: info)

    def test_plan_ignored_without_unified_memory(self):
        cfg = {"plan": {"enabled": True, "gpu_reserved_gb": 0.5, "shared_fraction": 0.5}}
        with mock.patch.dict(hardware.CONFIG.hub["hardware"], cfg):
            with self._with("discrete"):
                self.assertFalse(hardware.is_unified())
                self.assertIsNone(hardware.plan())
            with self._with("integrated"):
                self.assertTrue(hardware.is_unified())
                self.assertEqual(hardware.plan()["gpu_reserved_gb"], 0.5)


class TestSafety(unittest.TestCase):
    def test_file_types(self):
        self.assertEqual(sources.file_type_verdict("model.gguf")[0], "ok")
        self.assertEqual(sources.file_type_verdict("model.safetensors")[0], "ok")
        for bad in ("pytorch_model.bin", "model.pt", "x.ckpt", "setup.exe", "weights.zip", "run.ps1"):
            self.assertEqual(sources.file_type_verdict(bad)[0], "blocked", bad)

    def test_host_allowlist(self):
        pats = ["huggingface.co", "*.hf.co"]
        self.assertTrue(net.host_allowed("https://huggingface.co/x", pats))
        self.assertTrue(net.host_allowed("https://us.aws.cdn.hf.co/x", pats))
        self.assertFalse(net.host_allowed("http://huggingface.co/x", pats))  # not https
        self.assertFalse(net.host_allowed("https://huggingface.co.evil.com/x", pats))
        self.assertFalse(net.host_allowed("https://evilhf.co/x", pats))

    def test_modified_flag(self):
        self.assertTrue(discovery._annotate({"id": "someone/Model-Uncensored-GGUF"})["modified"])
        self.assertFalse(discovery._annotate({"id": "ggml-org/gpt-oss-20b-GGUF"})["modified"])


if __name__ == "__main__":
    unittest.main()
