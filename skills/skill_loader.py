"""技能加载器 - 动态发现和加载技能"""

from pathlib import Path
from typing import Optional
import importlib.util
import sys

from .skill_base import SkillBase


class SkillLoader:
    """发现和加载 PROJECT_ROOT/skills/ 目录中的技能"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.skills_dir = project_root / "skills"
        self._loaded_skills: dict[str, SkillBase] = {}

    def discover_skills(self) -> list[str]:
        """发现所有可用技能"""
        skills = []
        if not self.skills_dir.exists():
            return skills

        for entry in self.skills_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("_") and entry.name != "shared":
                if (entry / "SKILL.md").exists() or (entry / "skill.py").exists():
                    skills.append(entry.name)

        return skills

    def load_skill(self, skill_name: str) -> Optional[SkillBase]:
        """加载技能"""
        if skill_name in self._loaded_skills:
            return self._loaded_skills[skill_name]

        skill_dir = self.skills_dir / skill_name
        if not skill_dir.exists():
            return None

        skill_py = skill_dir / "skill.py"
        if skill_py.exists():
            skill_instance = self._load_python_skill(skill_dir, skill_py)
            if skill_instance:
                self._loaded_skills[skill_name] = skill_instance
                return skill_instance

        from .shell_skill import ShellSkill
        skill_instance = ShellSkill(skill_dir, self.project_root, skill_name)
        self._loaded_skills[skill_name] = skill_instance
        return skill_instance

    def _load_python_skill(self, skill_dir: Path, skill_py: Path) -> Optional[SkillBase]:
        """动态加载Python技能模块"""
        spec = importlib.util.spec_from_file_location("skill", skill_py)
        if not spec or not spec.loader:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules["skill"] = module
        spec.loader.exec_module(module)

        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, SkillBase) and obj != SkillBase:
                return obj(skill_dir, self.project_root)

        return None

    def get_skill(self, skill_name: str) -> Optional[SkillBase]:
        """获取技能实例"""
        return self.load_skill(skill_name)


# 全局技能加载器
_skill_loader: Optional[SkillLoader] = None


def get_skill_loader(project_root: Optional[Path] = None) -> SkillLoader:
    """获取全局技能加载器"""
    global _skill_loader

    if _skill_loader is None:
        if project_root is None:
            from config import PROJECT_ROOT
            project_root = PROJECT_ROOT
        _skill_loader = SkillLoader(project_root)

    return _skill_loader


def get_skill(skill_name: str) -> Optional[SkillBase]:
    """便捷函数：获取技能"""
    return get_skill_loader().get_skill(skill_name)