"""
存档翻译服务 - 处理玩家、载具、区块内容的翻译。

提供：
- 玩家数据翻译（身体部位、技能、特性、状态）
- 载具数据翻译（部件、类型）
- 区块内容翻译（容器、建筑）

@author: Cyicek
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, List, Any

from services.i18n_base import TranslationService
from services.log_service import log_service


class ArchiveTranslationService(TranslationService):
    """
    存档功能翻译服务。
    
    支持：
    - 玩家数据：身体部位、技能、特性、moodles
    - 载具数据：部件、类型
    - 区块内容：容器、建筑、区域
    """
    
    # 分类定义
    CATEGORY_PLAYER = "archive_player"
    CATEGORY_VEHICLE = "archive_vehicle"
    CATEGORY_CONTENT = "archive_content"
    
    def __init__(self):
        super().__init__()
        
        # 预加载常用分类
        self._prewarm_cache()
    
    def _prewarm_cache(self) -> None:
        """预热缓存 - 加载常用分类。"""
        # 预加载玩家数据翻译（最常用）
        self.load_category(self.CATEGORY_PLAYER, "zh_CN")
        self.load_category(self.CATEGORY_PLAYER, "en_US")
    
    # ============ 玩家数据翻译 ============
    
    def translate_body_part(self, part_id: str, locale: str = None) -> str:
        """
        翻译身体部位。
        
        Args:
            part_id: 部位 ID (e.g., "Head", "Torso_Upper")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            part_id, 
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=part_id
        )
    
    def translate_skill(self, skill_id: str, locale: str = None) -> str:
        """
        翻译技能。
        
        Args:
            skill_id: 技能 ID (e.g., "Fitness", "Strength")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            skill_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=skill_id
        )
    
    def translate_trait(self, trait_id: str, locale: str = None) -> str:
        """
        翻译特性。
        
        Args:
            trait_id: 特性 ID (e.g., "Brave", "Cowardly")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            trait_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=trait_id
        )
    
    def translate_moodle(self, moodle_id: str, locale: str = None) -> str:
        """
        翻译 moodle（状态效果）。
        
        Args:
            moodle_id: Moodie ID (e.g., "Hungry", "Thirsty")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            moodle_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=moodle_id
        )
    
    def translate_skill_group(self, group_id: str, locale: str = None) -> str:
        """
        翻译技能分组。
        
        Args:
            group_id: 分组 ID (e.g., "combat", "survival")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            group_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=group_id
        )
    
    def get_all_body_parts(self, locale: str = None) -> Dict[str, str]:
        """获取所有身体部位翻译。"""
        return self._get_category_section("body_parts", locale)
    
    def get_all_skills(self, locale: str = None) -> Dict[str, str]:
        """获取所有技能翻译。"""
        return self._get_category_section("skills", locale)
    
    def get_all_traits(self, locale: str = None) -> Dict[str, str]:
        """获取所有特性翻译。"""
        return self._get_category_section("traits", locale)
    
    def get_all_moodles(self, locale: str = None) -> Dict[str, str]:
        """获取所有 moodle 翻译。"""
        return self._get_category_section("moodles", locale)
    
    # ============ 载具数据翻译 ============
    
    def translate_vehicle_part(self, part_id: str, locale: str = None) -> str:
        """
        翻译载具部件。
        
        Args:
            part_id: 部件 ID (e.g., "Engine", "TireFrontLeft")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            part_id,
            category=self.CATEGORY_VEHICLE,
            locale=locale,
            default=part_id
        )
    
    def translate_vehicle_type(self, type_id: str, locale: str = None) -> str:
        """
        翻译载具类型。
        
        Args:
            type_id: 类型 ID (e.g., "Base.CarNormal", "Base.PickUpTruck")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            type_id,
            category=self.CATEGORY_VEHICLE,
            locale=locale,
            default=type_id
        )
    
    def translate_part_category(self, category_id: str, locale: str = None) -> str:
        """
        翻译部件分类。
        
        Args:
            category_id: 分类 ID (e.g., "engine", "doors")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            category_id,
            category=self.CATEGORY_VEHICLE,
            locale=locale,
            default=category_id
        )
    
    def get_all_vehicle_parts(self, locale: str = None) -> Dict[str, str]:
        """获取所有载具部件翻译。"""
        return self._get_category_section("parts", locale)
    
    def get_all_vehicle_types(self, locale: str = None) -> Dict[str, str]:
        """获取所有载具类型翻译。"""
        return self._get_category_section("types", locale)
    
    # ============ 区块内容翻译 ============
    
    def translate_container(self, container_id: str, locale: str = None) -> str:
        """
        翻译容器类型。
        
        Args:
            container_id: 容器 ID (e.g., "fridge", "crate")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            container_id,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=container_id
        )
    
    def translate_building(self, building_id: str, locale: str = None) -> str:
        """
        翻译建筑类型。
        
        Args:
            building_id: 建筑 ID (e.g., "house", "store")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            building_id,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=building_id
        )
    
    def translate_zone(self, zone_id: str, locale: str = None) -> str:
        """
        翻译区域类型。
        
        Args:
            zone_id: 区域 ID (e.g., "TownZone", "Forest")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            zone_id,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=zone_id
        )
    
    def translate_object_type(self, object_type: str, locale: str = None) -> str:
        """
        翻译对象类型。
        
        Args:
            object_type: 对象类型 (e.g., "IsoDoor", "IsoWindow")
            locale: 语言代码
            
        Returns:
            翻译后的名称
        """
        return self.get(
            object_type,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=object_type
        )
    
    def get_all_containers(self, locale: str = None) -> Dict[str, str]:
        """获取所有容器翻译。"""
        return self._get_category_section("containers", locale)
    
    def get_all_buildings(self, locale: str = None) -> Dict[str, str]:
        """获取所有建筑翻译。"""
        return self._get_category_section("buildings", locale)
    
    def get_all_zones(self, locale: str = None) -> Dict[str, str]:
        """获取所有区域翻译。"""
        return self._get_category_section("zones", locale)
    
    # ============ 批量翻译 ============
    
    def translate_body_parts_batch(
        self, 
        part_ids: List[str], 
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译身体部位。"""
        return self.get_batch(part_ids, self.CATEGORY_PLAYER, locale)
    
    def translate_skills_batch(
        self, 
        skill_ids: List[str], 
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译技能。"""
        return self.get_batch(skill_ids, self.CATEGORY_PLAYER, locale)
    
    def translate_traits_batch(
        self, 
        trait_ids: List[str], 
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译特性。"""
        return self.get_batch(trait_ids, self.CATEGORY_PLAYER, locale)
    
    def translate_vehicle_parts_batch(
        self,
        part_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译载具部件。"""
        return self.get_batch(part_ids, self.CATEGORY_VEHICLE, locale)
    
    def translate_vehicle_types_batch(
        self,
        type_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译载具类型。"""
        return self.get_batch(type_ids, self.CATEGORY_VEHICLE, locale)
    
    def translate_containers_batch(
        self,
        container_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译容器。"""
        return self.get_batch(container_ids, self.CATEGORY_CONTENT, locale)
    
    def translate_buildings_batch(
        self,
        building_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        """批量翻译建筑。"""
        return self.get_batch(building_ids, self.CATEGORY_CONTENT, locale)
    
    # ============ 内部方法 ============
    
    def _get_category_section(
        self, 
        section: str, 
        locale: str = None
    ) -> Dict[str, str]:
        """
        获取分类的某个子部分。
        
        Args:
            section: 子部分名称 (e.g., "body_parts", "skills")
            locale: 语言代码
            
        Returns:
            子部分字典
        """
        locale = locale or self._current_locale
        
        # 确保已加载
        if not self.load_category(self.CATEGORY_PLAYER, locale):
            return {}
        
        cache_key = self._compute_cache_key(self.CATEGORY_PLAYER, locale)
        
        with self._lock:
            data = self._cache_l1.get(cache_key, {})
            return data.get(section, {})
    
    def _load_from_source(
        self, 
        category: str, 
        locale: str
    ) -> Optional[Dict[str, Any]]:
        """从源文件加载翻译数据。"""
        # 确定子目录
        if category == self.CATEGORY_PLAYER:
            subdir = "archive"
            filename = f"player_{locale}.json"
        elif category == self.CATEGORY_VEHICLE:
            subdir = "archive"
            filename = f"vehicle_{locale}.json"
        elif category == self.CATEGORY_CONTENT:
            subdir = "archive"
            filename = f"content_{locale}.json"
        else:
            # 回退到基类行为
            return super()._load_from_source(category, locale)
        
        file_path = self._base_path / subdir / filename
        
        if not file_path.exists():
            log_service.runtime_debug(
                f"[ArchiveI18n] Source file not found: {file_path}",
                "TranslationService"
            )
            return None
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 更新条目计数
            data["_meta"]["entry_count"] = self._count_entries(data)
            
            log_service.runtime_debug(
                f"[ArchiveI18n] Loaded {category}/{locale} from {filename}",
                "TranslationService"
            )
            return data
        
        except json.JSONDecodeError as exc:
            log_service.runtime_debug(
                f"[ArchiveI18n] JSON parse error in {file_path}: {exc}",
                "TranslationService"
            )
            return None
        except Exception as exc:
            log_service.runtime_debug(
                f"[ArchiveI18n] Load error: {exc}",
                "TranslationService"
            )
            return None
    
# 全局单例
archive_i18n = ArchiveTranslationService()


# 便捷函数
def translate_body_part(part_id: str, locale: str = None) -> str:
    """翻译身体部位。"""
    return archive_i18n.translate_body_part(part_id, locale)


def translate_skill(skill_id: str, locale: str = None) -> str:
    """翻译技能。"""
    return archive_i18n.translate_skill(skill_id, locale)


def translate_trait(trait_id: str, locale: str = None) -> str:
    """翻译特性。"""
    return archive_i18n.translate_trait(trait_id, locale)


def translate_moodle(moodle_id: str, locale: str = None) -> str:
    """翻译 moodle。"""
    return archive_i18n.translate_moodle(moodle_id, locale)


def translate_vehicle_part(part_id: str, locale: str = None) -> str:
    """翻译载具部件。"""
    return archive_i18n.translate_vehicle_part(part_id, locale)


def translate_vehicle_type(type_id: str, locale: str = None) -> str:
    """翻译载具类型。"""
    return archive_i18n.translate_vehicle_type(type_id, locale)


def translate_container(container_id: str, locale: str = None) -> str:
    """翻译容器。"""
    return archive_i18n.translate_container(container_id, locale)


def translate_building(building_id: str, locale: str = None) -> str:
    """翻译建筑。"""
    return archive_i18n.translate_building(building_id, locale)
