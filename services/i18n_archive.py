"""Documentation translated to English.

Documentation translated to English.
Documentation translated to English.
Documentation translated to English.
Documentation translated to English.

@author: Cyicek"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, List, Any

from services.i18n_base import TranslationService
from services.log_service import log_service


class ArchiveTranslationService(TranslationService):
    """Documentation translated to English.

Documentation translated to English.
moodles
Documentation translated to English.
Documentation translated to English."""
    
    # Comment translated to English.
    CATEGORY_PLAYER = "archive_player"
    CATEGORY_VEHICLE = "archive_vehicle"
    CATEGORY_CONTENT = "archive_content"
    
    def __init__(self):
        super().__init__()
        
        # Comment translated to English.
        self._prewarm_cache()
    
    def _prewarm_cache(self) -> None:
        """Preload common categories for default locales."""
        self.load_category(self.CATEGORY_PLAYER, "zh_CN")
        self.load_category(self.CATEGORY_PLAYER, "en_US")
    
    # ============ ============
    
    def translate_body_part(self, part_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
part_id: ID (e.g., "Head", "Torso_Upper")
locale

Returns
Documentation translated to English."""
        return self.get(
            part_id, 
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=part_id
        )
    
    def translate_skill(self, skill_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
skill_id: ID (e.g., "Fitness", "Strength")
locale

Returns
Documentation translated to English."""
        return self.get(
            skill_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=skill_id
        )
    
    def translate_trait(self, trait_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
trait_id: ID (e.g., "Brave", "Cowardly")
locale

Returns
Documentation translated to English."""
        return self.get(
            trait_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=trait_id
        )
    
    def translate_moodle(self, moodle_id: str, locale: str = None) -> str:
        """moodle

Args
moodle_id: Moodie ID (e.g., "Hungry", "Thirsty")
locale

Returns
Documentation translated to English."""
        return self.get(
            moodle_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=moodle_id
        )
    
    def translate_skill_group(self, group_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
group_id: ID (e.g., "combat", "survival")
locale

Returns
Documentation translated to English."""
        return self.get(
            group_id,
            category=self.CATEGORY_PLAYER,
            locale=locale,
            default=group_id
        )
    
    def get_all_body_parts(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("body_parts", locale)
    
    def get_all_skills(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("skills", locale)
    
    def get_all_traits(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("traits", locale)
    
    def get_all_moodles(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("moodles", locale)
    
    # ============ ============
    
    def translate_vehicle_part(self, part_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
part_id: ID (e.g., "Engine", "TireFrontLeft")
locale

Returns
Documentation translated to English."""
        return self.get(
            part_id,
            category=self.CATEGORY_VEHICLE,
            locale=locale,
            default=part_id
        )
    
    def translate_vehicle_type(self, type_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
type_id: ID (e.g., "Base.CarNormal", "Base.PickUpTruck")
locale

Returns
Documentation translated to English."""
        return self.get(
            type_id,
            category=self.CATEGORY_VEHICLE,
            locale=locale,
            default=type_id
        )
    
    def translate_part_category(self, category_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
category_id: ID (e.g., "engine", "doors")
locale

Returns
Documentation translated to English."""
        return self.get(
            category_id,
            category=self.CATEGORY_VEHICLE,
            locale=locale,
            default=category_id
        )
    
    def get_all_vehicle_parts(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("parts", locale)
    
    def get_all_vehicle_types(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("types", locale)
    
    # ============ ============
    
    def translate_container(self, container_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
container_id: ID (e.g., "fridge", "crate")
locale

Returns
Documentation translated to English."""
        return self.get(
            container_id,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=container_id
        )
    
    def translate_building(self, building_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
building_id: ID (e.g., "house", "store")
locale

Returns
Documentation translated to English."""
        return self.get(
            building_id,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=building_id
        )
    
    def translate_zone(self, zone_id: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
zone_id: ID (e.g., "TownZone", "Forest")
locale

Returns
Documentation translated to English."""
        return self.get(
            zone_id,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=zone_id
        )
    
    def translate_object_type(self, object_type: str, locale: str = None) -> str:
        """Documentation translated to English.

Args
object_type: (e.g., "IsoDoor", "IsoWindow")
locale

Returns
Documentation translated to English."""
        return self.get(
            object_type,
            category=self.CATEGORY_CONTENT,
            locale=locale,
            default=object_type
        )
    
    def get_all_containers(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("containers", locale)
    
    def get_all_buildings(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("buildings", locale)
    
    def get_all_zones(self, locale: str = None) -> Dict[str, str]:
        return self._get_category_section("zones", locale)
    
    # ============ ============
    
    def translate_body_parts_batch(
        self, 
        part_ids: List[str], 
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(part_ids, self.CATEGORY_PLAYER, locale)
    
    def translate_skills_batch(
        self, 
        skill_ids: List[str], 
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(skill_ids, self.CATEGORY_PLAYER, locale)
    
    def translate_traits_batch(
        self, 
        trait_ids: List[str], 
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(trait_ids, self.CATEGORY_PLAYER, locale)
    
    def translate_vehicle_parts_batch(
        self,
        part_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(part_ids, self.CATEGORY_VEHICLE, locale)
    
    def translate_vehicle_types_batch(
        self,
        type_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(type_ids, self.CATEGORY_VEHICLE, locale)
    
    def translate_containers_batch(
        self,
        container_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(container_ids, self.CATEGORY_CONTENT, locale)
    
    def translate_buildings_batch(
        self,
        building_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(building_ids, self.CATEGORY_CONTENT, locale)

    def translate_zones_batch(
        self,
        zone_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(zone_ids, self.CATEGORY_CONTENT, locale)

    def translate_object_types_batch(
        self,
        object_type_ids: List[str],
        locale: str = None
    ) -> Dict[str, str]:
        return self.get_batch(object_type_ids, self.CATEGORY_CONTENT, locale)
    
    # ============ ============
    
    def _get_category_section(
        self, 
        section: str, 
        locale: str = None
    ) -> Dict[str, str]:
        """Documentation translated to English.

Args
section: (e.g., "body_parts", "skills")
locale

Returns
Documentation translated to English."""
        locale = locale or self._current_locale
        
        # Comment translated to English.
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
        """Load archive translation data from source files."""
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
            # Comment translated to English.
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
            
            # Comment translated to English.
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
    
# Comment translated to English.
archive_i18n = ArchiveTranslationService()


# Comment translated to English.
def translate_body_part(part_id: str, locale: str = None) -> str:
    return archive_i18n.translate_body_part(part_id, locale)


def translate_skill(skill_id: str, locale: str = None) -> str:
    return archive_i18n.translate_skill(skill_id, locale)


def translate_trait(trait_id: str, locale: str = None) -> str:
    return archive_i18n.translate_trait(trait_id, locale)


def translate_moodle(moodle_id: str, locale: str = None) -> str:
    return archive_i18n.translate_moodle(moodle_id, locale)


def translate_vehicle_part(part_id: str, locale: str = None) -> str:
    return archive_i18n.translate_vehicle_part(part_id, locale)


def translate_vehicle_type(type_id: str, locale: str = None) -> str:
    return archive_i18n.translate_vehicle_type(type_id, locale)


def translate_container(container_id: str, locale: str = None) -> str:
    return archive_i18n.translate_container(container_id, locale)


def translate_building(building_id: str, locale: str = None) -> str:
    return archive_i18n.translate_building(building_id, locale)
