"""Shell技能包装器 - 用于只有脚本的技能"""

from pathlib import Path
from typing import Any
import subprocess

from .skill_base import SkillBase


class ShellSkill(SkillBase):
    """Shell技能包装器"""

    def __init__(self, skill_dir: Path, project_root: Path, skill_name: str):
        self._name = skill_name
        super().__init__(skill_dir, project_root)
        self._load_description()

    def _load_description(self) -> None:
        """从SKILL.md加载描述"""
        skill_md = self.skill_dir / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text()
            if "---" in content:
                import yaml
                parts = content.split("---")
                if len(parts) >= 2:
                    try:
                        metadata = yaml.safe_load(parts[1])
                        self._description = metadata.get("description", "")
                    except:
                        self._description = ""
            else:
                self._description = ""
        else:
            self._description = ""

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    def execute(self, **kwargs) -> dict[str, Any]:
        """执行技能"""
        script_name = kwargs.get("script", self._get_default_script())
        args = kwargs.get("args", [])
        timeout = kwargs.get("timeout", 120)

        try:
            result = self.run_script(script_name, args, timeout)
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Timeout expired"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_default_script(self) -> str:
        """获取默认脚本"""
        default_scripts = {
            "kernel-fault-injection": "run_fault_injection.sh",
            "kernel-build": "build_kernel.sh",
            "qemu-test": "boot_kernel.sh",
        }
        return default_scripts.get(self.name, "run.sh")