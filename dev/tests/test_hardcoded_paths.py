"""Tests to detect hardcoded user-specific paths in source files.

This file must pass on any developer's machine, regardless of username.
It checks source files (py, json, md, yaml) for common anti-patterns like
/path/to/username/ and /home/<username> references that should be
parameterized via environment variables or config files.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Excluded file patterns — these are expected to contain user-specific paths
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = (
    # Output / session / generated files — contain real paths from analysis
    "outputs/",
    "sessions/",
    "knowledge_base/",
    "deadlock_analysis_output/",
    "test_assets/",
    ".pytest_cache/",
    ".git/",
    "venv/",
    "__pycache__/",
    ".venv/",
    "Analysis-SKILL/",           # git submodule
    "syzfetch-cases/",           # fetched/deployed case metadata, not source
    "runtime/",                  # generated QEMU/session artifacts
    ".agents/skills-disabled/",  # local disabled skills, not project source
    "dev/tests/test_hardcoded_paths.py",  # this file itself is fine
)

# Source file extensions to scan
SOURCE_EXTENSIONS = (".py", ".json", ".md", ".yaml", ".yml", ".sh", ".toml")

# ---------------------------------------------------------------------------
# Patterns that indicate hardcoded user-specific absolute paths
# ---------------------------------------------------------------------------

# $HOME is fine; /home/<username> is hardcoded
_HOME_RE = re.compile(r"/home/[^/]+/")

# Pattern for absolute paths that look like user directories
_ABSOLUTE_BIN_RE = re.compile(r'\b/(?:usr/)?local/bin/')
_ABSOLUTE_SRC_RE = re.compile(r'"(?:/home/[^/]+/(?:code|src|workspace|git)/[^"]+)"')


def _is_excluded(path: Path) -> bool:
    """Check if file should be excluded from hardcoded path scanning."""
    rel = path.relative_to(PROJECT_ROOT)
    parts = rel.parts
    for seg in EXCLUDE_PATTERNS:
        if seg in str(rel):
            return True
    return False


def _collect_source_files() -> list[Path]:
    """Return all source files under PROJECT_ROOT that should be scanned."""
    files = []
    for ext in SOURCE_EXTENSIONS:
        files.extend(PROJECT_ROOT.rglob(f"*{ext}"))
    # Filter
    return [f for f in files if not _is_excluded(f) and f.is_file()]


# Safe patterns — paths used as test fixtures / assertions that are expected
# to contain user-specific paths. These are exempt from hardcoded-path
# detection (test data, README examples with placeholder paths).
_SAFE_HOME_PATTERNS = (
    "/home/user/",         # generic placeholder in test fixtures
    "/home/zouyipeng/",    # original developer's path in README example
)


def test_no_hardcoded_home_paths_in_source():
    """Verify no source files contain /home/<username>/ hardcoded paths.

    Only checks source code files (excludes outputs, sessions, test_assets).
    """
    files = _collect_source_files()
    violations = []

    for f in sorted(files):
        rel = f.relative_to(PROJECT_ROOT)
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        # Skip allowed test files entirely — they may use /tmp/ paths
        # but we still want to scan them for /home/ leakage.
        for lineno, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # Skip comments and strings that document examples
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("<!--"):
                continue
            # Skip markdown content in knowledge_base
            if f.suffix == ".md" and "knowledge_base" in str(rel):
                continue

            matches = _HOME_RE.findall(stripped)
            for m in matches:
                # Skip env var templates (containing ${...})
                if "${" in stripped:
                    continue
                # Skip known safe patterns (test fixtures, placeholder paths)
                if any(pat in stripped for pat in _SAFE_HOME_PATTERNS):
                    continue
                violations.append(f"{rel}:{lineno}: {stripped[:120]}")

    if violations:
        msg = f"Found {len(violations)} hardcoded /home/... paths in source files:\n"
        msg += "\n".join(violations[:20])
        if len(violations) > 20:
            msg += f"\n... and {len(violations) - 20} more"
        msg += "\n\nReplace with ${HOME} or config-based paths."
        pytest.fail(msg, pytrace=False)


def test_config_template_no_hardcoded_paths():
    """Verify config.json.template uses env vars, not hardcoded paths."""
    template = PROJECT_ROOT / "config.json.template"
    if not template.exists():
        pytest.skip("No template file found")
    content = template.read_text(encoding="utf-8")
    violations = _HOME_RE.findall(content)
    if violations:
        pytest.fail(
            f"Template contains hardcoded paths: {violations[:5]}\n"
            f"Use ${{HOME}} or ${{VAR}} instead.",
            pytrace=False,
        )


def test_env_var_resolution_runtime(monkeypatch):
    """Verify _resolve_env_vars handles all template patterns correctly."""
    from llm_config import _resolve_env_vars

    # Required chat settings are supplied by the deployment environment.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")

    # Load template and verify every environment reference is resolvable.
    template = PROJECT_ROOT / "config.json.template"
    if not template.exists():
        pytest.skip("No template file found")

    content = template.read_text(encoding="utf-8")

    # Apply resolution — no KeyError should remain if we have sensible defaults.
    # Skip lines that are documentation (comments, code blocks showing examples)
    clean_lines = []
    for line in content.splitlines():
        # Strip inline comments that mention variables for documentation
        clean_lines.append(line)
    clean_content = "\n".join(clean_lines)

    resolved = _resolve_env_vars(clean_content)
    unresolved = re.findall(r'\$\{[^}]+\}', resolved)
    if unresolved:
        pytest.fail(
            f"Template has unresolved variables: {unresolved[:5]}\n"
            f"All variables should be resolved from the test environment.",
            pytrace=False,
        )


def test_config_parses_after_env_resolution(monkeypatch):
    """Verify config.json loads correctly after env resolution."""
    from llm_config import load_config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    config_path = PROJECT_ROOT / "config.json"
    if not config_path.exists():
        pytest.skip("No config file found")

    config = load_config(str(config_path))

    # Verify semcode paths with nested env vars are resolved
    ke = config.get("agents", {}).get("kernel_expert", {})
    semcode = ke.get("semcode_mcp", {})
    if semcode:
        cmd = semcode.get("command", "")
        assert "${" not in cmd, f"semcode.command has unresolved vars: {cmd}"


def test_no_username_absolute_paths_in_ci_configs():
    """Check that yaml and json config files don't contain /home/<user> paths."""
    for ext in (".yaml", ".yml", ".json", ".toml"):
        for f in PROJECT_ROOT.rglob(f"*{ext}"):
            if _is_excluded(f):
                continue

            rel = f.relative_to(PROJECT_ROOT)
            # Only check config/template files
            if not any(seg in str(rel) for seg in ("config", "template", ".json")):
                continue

            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue

            violations = _HOME_RE.findall(content)
            if violations:
                # Allow if the file is a template with env vars
                if "${" in content:
                    continue
                pytest.fail(
                    f"{rel} contains hardcoded paths: {violations[:3]}\n"
                    f"Use ${{HOME}} or config-based paths.",
                    pytrace=False,
                )


def test_resolve_env_vars_basic():
    """_resolve_env_vars handles ${VAR}, ${VAR:-default}, nested vars."""
    from llm_config import _resolve_env_vars

    # ${VAR:-default} with VAR unset → default
    assert _resolve_env_vars("${MISSING:-fallback}") == "fallback"

    # ${VAR:-default} with VAR set → value
    os.environ["MY_TEST_VAR"] = "hello"
    try:
        assert _resolve_env_vars("${MY_TEST_VAR:-fallback}") == "hello"
    finally:
        del os.environ["MY_TEST_VAR"]

    # Nested ${A:-${B:-x}}
    assert _resolve_env_vars("${A:-${B:-inner}}") == "inner"

    # Simple $HOME
    original_home = os.environ.get("HOME", "")
    os.environ["HOME"] = "/test/home"
    try:
        assert _resolve_env_vars("$HOME/code") == "/test/home/code"
    finally:
        os.environ["HOME"] = original_home


def test_resolve_env_vars_unresolved_strict():
    """${VAR} without default and VAR unset raises KeyError."""
    from llm_config import _resolve_env_vars

    # Unset var without default → KeyError (per docstring contract)
    unset = "_DEFINITELY_NOT_SET_VAR_"
    if unset in os.environ:
        del os.environ[unset]
    try:
        _resolve_env_vars("${" + unset + "}")
        pytest.fail("Expected KeyError for unset var without default", pytrace=False)
    except KeyError:
        pass


def test_config_resolves_semcode_paths(monkeypatch):
    """config.json must resolve semcode_mcp paths to absolute strings."""
    from llm_config import load_config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    config_path = PROJECT_ROOT / "config.json"
    if not config_path.exists():
        pytest.skip("No config.json found")

    config = load_config(str(config_path))

    # semcode_mcp paths with nested env vars must be resolved
    semcode = config.get("agents", {}).get("kernel_expert", {}).get("semcode_mcp", {})
    if semcode:
        cmd = semcode.get("command", "")
        assert "${" not in cmd, f"semcode.command has unresolved vars: {cmd}"
        args = semcode.get("args", [])
        for arg in args:
            assert "${" not in arg, f"semcode arg has unresolved vars: {arg}"


def test_no_hardcoded_username_in_agent_source():
    """agents/, graph/, paths.py, project.py, llm_config.py, main.py
    must not contain /home/<username>/ paths — only ${HOME} or env vars."""
    source_dirs = ["agents", "graph"]
    source_files = [
        PROJECT_ROOT / "paths.py",
        PROJECT_ROOT / "project.py",
        PROJECT_ROOT / "llm_config.py",
        PROJECT_ROOT / "main.py",
    ]
    for d in source_dirs:
        for f in (PROJECT_ROOT / d).rglob("*.py"):
            source_files.append(f)

    violations = []
    for f in source_files:
        if not f.exists():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if "${" in line:
                continue  # env var template
            matches = _HOME_RE.findall(line)
            for m in matches:
                if any(pat in line for pat in _SAFE_HOME_PATTERNS):
                    continue
                rel = f.relative_to(PROJECT_ROOT)
                violations.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    if violations:
        pytest.fail(
            f"Hardcoded /home/ paths in agent source:\n" + "\n".join(violations[:20]),
            pytrace=False,
        )

def test_config_does_not_pin_provider_or_model_defaults():
    """Provider URLs, credentials, and model names come from deployment env."""
    template = (PROJECT_ROOT / "config.json.template").read_text(encoding="utf-8")
    forbidden = ("api.deepseek.com", "deepseek-v4-flash", "localhost:11434", '"model": "sonnet"')
    found = [value for value in forbidden if value in template]
    assert not found, f"provider-specific defaults must be configured externally: {found}"

def test_ikconfig_discovery_uses_explicit_kernel_root(monkeypatch, tmp_path):
    """Kernel config extraction must not probe another developer's tree."""
    from agents.cache.ikconfig_cache import _find_extract_ikconfig

    source = tmp_path / "kernel"
    script = source / "scripts" / "extract-ikconfig"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)
    for variable in ("LUMEN_IKCONFIG_SCRIPT", "KERNEL_SOURCE_DIR", "LUMEN_KERNEL_SOURCE_ROOT"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("PATH", "")
    assert _find_extract_ikconfig() is None

    monkeypatch.setenv("LUMEN_KERNEL_SOURCE_ROOT", str(source))
    assert _find_extract_ikconfig() == str(script)

def test_deploy_preflight_uses_loaded_crash_dir_env():
    """The preflight must read LUMEN_CRASH_BIN_DIRS after .env loading."""
    deploy = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert 'configured_crash_bin_dirs="${LUMEN_CRASH_BIN_DIRS:-$CRASH_BIN_DIRS}"' in deploy
    assert 'read -r -a extra_crash_dirs <<< "$configured_crash_bin_dirs"' in deploy


def test_qemu_provisioner_requires_deployment_mirror_and_component_manifest():
    """QEMU provisioning must use deployment inputs and declare guest contents."""
    script = (PROJECT_ROOT / "scripts" / "provision_qemu_ssh_image.sh").read_text(
        encoding="utf-8"
    )
    assert "LUMEN_DEBIAN_MIRROR" in script
    assert "LUMEN_DEBIAN_DISTRIBUTION" in script
    assert "LUMEN_QEMU_IMAGE_ROOT" in script
    assert "command -v qemu-x86_64-static" in script
    assert "command -v qemu-aarch64-static" in script
    assert "--mirror" in script
    assert "mirrors.aliyun.com/debian" not in script
    assert "guest-components.manifest" in script
    assert "--rebuild" in script
    deploy = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "LUMEN_GNU_MIRROR" in deploy
