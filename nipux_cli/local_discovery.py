"""Local hardware, runtime, and model discovery for first-run setup."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence


LOCAL_MODEL_SUGGESTIONS = (
    "qwen3.6:27b Q4_K_M",
    "qwen3.6:27b Q5_K_M",
    "qwen3.6:27b Q8_0",
    "qwen3.6:35b-a3b Q4_K_M",
    "qwen3.6:35b-a3b Q5_K_M",
)

LLAMA_CPP_DISPLAY_NAME = "llama" + ".cpp"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class HardwareInfo:
    system: str
    arch: str
    cpu: str
    accelerators: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        parts = [self.system, self.arch]
        if self.cpu:
            parts.append(self.cpu)
        if self.accelerators:
            parts.append(", ".join(self.accelerators))
        return " · ".join(part for part in parts if part)


@dataclass(frozen=True)
class RuntimeInfo:
    name: str
    installed: bool
    running: bool
    command: str = ""
    base_url: str = ""
    models: tuple[str, ...] = ()
    detail: str = ""
    install_hint: str = ""


@dataclass(frozen=True)
class LocalModelConfig:
    runtime: str
    model: str
    base_url: str
    reason: str


@dataclass(frozen=True)
class LocalDiscoveryReport:
    hardware: HardwareInfo
    runtimes: tuple[RuntimeInfo, ...]
    recommended: LocalModelConfig | None = None
    suggestions: tuple[str, ...] = LOCAL_MODEL_SUGGESTIONS

    @property
    def installed_runtimes(self) -> tuple[RuntimeInfo, ...]:
        return tuple(runtime for runtime in self.runtimes if runtime.installed)

    @property
    def running_runtimes(self) -> tuple[RuntimeInfo, ...]:
        return tuple(runtime for runtime in self.runtimes if runtime.running)


Runner = Callable[[Sequence[str], float], CommandResult]


def default_command_runner(argv: Sequence[str], timeout: float = 1.0) -> CommandResult:
    if not argv:
        return CommandResult(127, "", "empty command")
    if shutil.which(argv[0]) is None:
        return CommandResult(127, "", f"{argv[0]} not found")
    try:
        completed = subprocess.run(
            list(argv),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(124, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


@lru_cache(maxsize=1)
def cached_local_discovery() -> LocalDiscoveryReport:
    return discover_local_environment()


def clear_local_discovery_cache() -> None:
    cached_local_discovery.cache_clear()


def discover_local_environment(
    *,
    runner: Runner = default_command_runner,
    home: Path | None = None,
) -> LocalDiscoveryReport:
    home = Path.home() if home is None else Path(home).expanduser()
    hardware = detect_hardware(runner=runner)
    runtimes = (
        detect_ollama(runner=runner),
        detect_lm_studio(home=home),
        detect_vllm(home=home),
        detect_llama_cpp(home=home),
    )
    recommended = choose_recommended_local_model(runtimes)
    return LocalDiscoveryReport(hardware=hardware, runtimes=runtimes, recommended=recommended)


def detect_hardware(*, runner: Runner = default_command_runner) -> HardwareInfo:
    system = platform.system() or "Unknown"
    arch = platform.machine() or "unknown"
    cpu = platform.processor() or ""
    accelerators: list[str] = []
    if system == "Darwin":
        cpu_result = runner(("sysctl", "-n", "machdep.cpu.brand_string"), 0.5)
        if cpu_result.returncode == 0 and cpu_result.stdout.strip():
            cpu = cpu_result.stdout.strip()
        if arch == "arm64":
            accelerators.append("Apple Silicon")
        else:
            accelerators.append("Mac GPU")
    nvidia = runner(("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"), 0.8)
    if nvidia.returncode == 0:
        for line in nvidia.stdout.splitlines():
            cleaned = " ".join(line.split())
            if cleaned:
                accelerators.append(f"NVIDIA {cleaned}")
    rocm = runner(("rocm-smi", "--showproductname"), 0.8)
    if rocm.returncode == 0:
        for line in rocm.stdout.splitlines():
            lowered = line.lower()
            if "gpu" in lowered or "card" in lowered or "series" in lowered:
                cleaned = " ".join(line.replace(":", " ").split())
                if cleaned and cleaned not in accelerators:
                    accelerators.append(f"AMD {cleaned}")
    lspci = runner(("lspci",), 0.8)
    if lspci.returncode == 0:
        for line in lspci.stdout.splitlines():
            lowered = line.lower()
            if not any(marker in lowered for marker in ("vga", "3d controller", "display controller")):
                continue
            vendor = ""
            if "nvidia" in lowered:
                vendor = "NVIDIA"
            elif "amd" in lowered or "advanced micro devices" in lowered or "ati" in lowered:
                vendor = "AMD"
            elif "intel" in lowered:
                vendor = "Intel"
            if vendor:
                accelerators.append(_dedupe_hardware_label(vendor, line))
    return HardwareInfo(system=system, arch=arch, cpu=cpu, accelerators=_stable_unique(accelerators))


def detect_ollama(*, runner: Runner = default_command_runner) -> RuntimeInfo:
    command = shutil.which("ollama") or ""
    installed = bool(command)
    result = runner(("ollama", "list"), 1.0) if installed else CommandResult(127, "", "ollama not found")
    models = parse_ollama_list(result.stdout) if result.returncode == 0 else ()
    running = result.returncode == 0
    detail = "running" if running else (result.stderr.strip() or result.stdout.strip() or "not running")
    return RuntimeInfo(
        name="Ollama",
        installed=installed,
        running=running,
        command=command,
        base_url="http://localhost:11434/v1",
        models=models,
        detail=detail,
        install_hint="Install Ollama, then pull a model such as qwen3.6:27b in a quantization that fits your machine.",
    )


def detect_lm_studio(*, home: Path) -> RuntimeInfo:
    command = shutil.which("lms") or ""
    installed = bool(command) or _path_exists_any(
        [
            Path("/Applications/LM Studio.app"),
            home / ".cache" / "lm-studio",
            home / "Library" / "Application Support" / "LM Studio",
        ]
    )
    endpoint_models = probe_openai_models("http://localhost:1234/v1")
    models = endpoint_models or scan_lm_studio_models(home)
    running = bool(endpoint_models)
    detail = "OpenAI server responding" if running else ("installed" if installed else "not found")
    return RuntimeInfo(
        name="LM Studio",
        installed=installed,
        running=running,
        command=command,
        base_url="http://localhost:1234/v1",
        models=models,
        detail=detail,
        install_hint="Install LM Studio, download a local chat model, then start its OpenAI-compatible local server.",
    )


def detect_vllm(*, home: Path) -> RuntimeInfo:
    command = shutil.which("vllm") or ""
    installed = bool(command)
    endpoint_models = probe_openai_models("http://localhost:8000/v1")
    models = endpoint_models or scan_huggingface_models(home)
    running = bool(endpoint_models)
    detail = "OpenAI server responding" if running else ("installed" if installed else "not found")
    return RuntimeInfo(
        name="vLLM",
        installed=installed,
        running=running,
        command=command,
        base_url="http://localhost:8000/v1",
        models=models,
        detail=detail,
        install_hint="Install vLLM and serve a local model with its OpenAI-compatible API server.",
    )


def detect_llama_cpp(*, home: Path) -> RuntimeInfo:
    command = shutil.which("llama-server") or shutil.which("llama-cli") or ""
    installed = bool(command)
    endpoint_models = probe_openai_models("http://localhost:8080/v1")
    ggufs = scan_gguf_models(home)
    models = endpoint_models or ggufs
    running = bool(endpoint_models)
    detail = "OpenAI server responding" if running else ("installed" if installed else "not found")
    return RuntimeInfo(
        name=LLAMA_CPP_DISPLAY_NAME,
        installed=installed or bool(ggufs),
        running=running,
        command=command,
        base_url="http://localhost:8080/v1",
        models=models,
        detail=detail,
        install_hint=(
            f"Install {LLAMA_CPP_DISPLAY_NAME}, download a GGUF model, then run llama-server "
            "with the OpenAI-compatible endpoint."
        ),
    )


def choose_recommended_local_model(runtimes: Iterable[RuntimeInfo]) -> LocalModelConfig | None:
    ordered = list(runtimes)
    for runtime in ordered:
        if runtime.running and runtime.models:
            model = runtime.models[0]
            return LocalModelConfig(runtime.name, model, runtime.base_url, "running local endpoint with installed model")
    for runtime in ordered:
        if runtime.installed and runtime.models:
            model = runtime.models[0]
            return LocalModelConfig(runtime.name, model, runtime.base_url, "installed runtime with local model")
    for runtime in ordered:
        if runtime.running:
            return LocalModelConfig(runtime.name, "local-model", runtime.base_url, "running local endpoint")
    return None


def recommended_local_model_config() -> LocalModelConfig | None:
    return cached_local_discovery().recommended


def probe_openai_models(base_url: str, *, timeout: float = 0.5) -> tuple[str, ...]:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer local-no-key"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(256_000).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return ()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return ()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return ()
    models: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("id") or item.get("name") or "").strip()
        if name:
            models.append(name)
    return _stable_unique(models)


def parse_ollama_list(text: str) -> tuple[str, ...]:
    models: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("name "):
            continue
        first = line.split(maxsplit=1)[0].strip()
        if first and first.lower() != "name":
            models.append(first)
    return _stable_unique(models)


def scan_lm_studio_models(home: Path, *, limit: int = 80) -> tuple[str, ...]:
    roots = [
        home / ".cache" / "lm-studio" / "models",
        home / "Library" / "Application Support" / "LM Studio" / "models",
    ]
    found: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in _limited_rglob(root, "*.gguf", limit=limit - len(found)):
            rel = path.relative_to(root)
            parts = rel.parts
            if len(parts) >= 3:
                found.append(f"{parts[0]}/{parts[1]}/{path.stem}")
            else:
                found.append(path.stem)
            if len(found) >= limit:
                break
    return _stable_unique(found)


def scan_huggingface_models(home: Path, *, limit: int = 80) -> tuple[str, ...]:
    root = home / ".cache" / "huggingface" / "hub"
    if not root.exists():
        return ()
    found: list[str] = []
    for path in root.glob("models--*"):
        name = path.name.removeprefix("models--").replace("--", "/")
        if name:
            found.append(name)
        if len(found) >= limit:
            break
    return _stable_unique(found)


def scan_gguf_models(home: Path, *, limit: int = 80) -> tuple[str, ...]:
    roots = [
        Path.cwd(),
        home / "models",
        home / "Models",
        home / "Downloads",
        home / ".cache" / "huggingface" / "hub",
    ]
    found: list[str] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in _limited_rglob(root, "*.gguf", limit=limit - len(found)):
            found.append(path.stem)
            if len(found) >= limit:
                break
    return _stable_unique(found)


def local_discovery_summary_lines(report: LocalDiscoveryReport, *, max_models: int = 5) -> list[str]:
    lines = [f"Hardware  {report.hardware.summary or 'unknown'}"]
    for runtime in report.runtimes:
        state = "running" if runtime.running else ("installed" if runtime.installed else "missing")
        models = ", ".join(runtime.models[:max_models])
        suffix = f" · {models}" if models else ""
        lines.append(f"{runtime.name:<9} {state}{suffix}")
    if report.recommended:
        lines.append(
            f"Using     {report.recommended.runtime} · {report.recommended.model} · {report.recommended.base_url}"
        )
    elif not report.installed_runtimes:
        lines.append("Install   " + "; ".join(runtime.install_hint for runtime in report.runtimes[:2]))
        lines.append("Models    " + ", ".join(report.suggestions[:3]))
    return lines


def _limited_rglob(root: Path, pattern: str, *, limit: int) -> tuple[Path, ...]:
    if limit <= 0:
        return ()
    found: list[Path] = []
    try:
        iterator = root.rglob(pattern)
        for path in iterator:
            if path.is_file():
                found.append(path)
                if len(found) >= limit:
                    break
    except OSError:
        return ()
    return tuple(found)


def _path_exists_any(paths: Iterable[Path]) -> bool:
    return any(path.exists() for path in paths)


def _dedupe_hardware_label(vendor: str, line: str) -> str:
    cleaned = re.sub(r"^\S+\s+", "", " ".join(line.split()))
    if vendor.lower() in cleaned.lower():
        return cleaned
    return f"{vendor} {cleaned}"


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value).split()).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return tuple(unique)
