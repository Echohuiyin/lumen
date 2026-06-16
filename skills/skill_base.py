"""技能抽象基类 - 所有内嵌技能的基础接口"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
import subprocess
import json


class SkillBase(ABC):
    """技能基类。

    每个技能提供：
    - name: 技能唯一标识
    - description: 技能描述
    - scripts: Shell/Python 脚本
    - config: 技能配置
    """

    def __init__(self, skill_dir: Path, project_root: Path):
        self.skill_dir = skill_dir
        self.project_root = project_root
        self._load_config()

    @property
    @abstractmethod
    def name(self) -> str:
        """技能唯一标识"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """技能描述"""
        pass

    def _load_config(self) -> None:
        """加载技能配置"""
        config_path = self.skill_dir / "config.json"
        if config_path.exists():
            self.config = json.loads(config_path.read_text())
        else:
            self.config = {}

    def get_script_path(self, script_name: str) -> Path:
        """获取脚本路径"""
        return self.skill_dir / "scripts" / script_name

    def run_script(
        self,
        script_name: str,
        args: list[str] = [],
        timeout: int = 120,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        """执行技能脚本"""
        script_path = self.get_script_path(script_name)
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        work_dir = cwd or self.skill_dir

        return subprocess.run(
            ["bash", str(script_path)] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(work_dir),
        )

    def run_python_script(
        self,
        script_name: str,
        args: list[str] = [],
        python_path: Optional[str] = None,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        """执行Python脚本"""
        import sys

        script_path = self.get_script_path(script_name)
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        interpreter = python_path or sys.executable

        return subprocess.run(
            [interpreter, str(script_path)] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @abstractmethod
    def execute(self, **kwargs) -> dict[str, Any]:
        """执行技能主功能"""
        pass

    def validate_inputs(self, **kwargs) -> bool:
        """验证输入参数"""
        return True