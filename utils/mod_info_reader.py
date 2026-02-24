import os
import re
from pathlib import Path
from collections import defaultdict


class ModInfoProcessor:
    """
    name=InGameMaps [Fort Knox Road]
    id=InGameMaps_FortKnoxRoad
    description=Add In-Game maps for Fort Knox Road
    poster=poster.png
    require=FortKnoxRoad,InGameMaps
    """
    """Steam Workshop MOD info processor (nested structure)."""

    def __init__(self):
        # self.file_path = Path(file_path)
        # self.mod_id = self._extract_mod_id()
        # self.folder_name = self._extract_folder_name()
        self.mod_info = {}
        self.name_index = {}
        self.steam_id_index = {}
        self.dir_name_index = {}

        self.mod_dir_name_list = []

    @staticmethod
    def _extract_mod_id(file_path):
        """Extract workshop ID from a path (cross-platform)."""
        parts = file_path.parts
        if 'workshop' in parts and 'content' in parts:
            content_index = parts.index('content') + 2
            return parts[content_index] if content_index < len(parts) else None
        return None

    def _find_mod_info(self, mod_info_list, root_path, c_depth=0, max_depth=5) -> list[Path]:
        root = Path(root_path)
        for _, path in enumerate(root.glob("*")):
            if path.name.startswith("."):
                continue
            path: Path
            if c_depth > max_depth:
                break
            elif path.name == "mod.info" and path.is_file():
                mod_info_list.append(path)
            elif path.is_dir():
                c_depth+=1
                self._find_mod_info(mod_info_list, path, c_depth, max_depth)

        # if not mod_info_list:
        #     print(f"none:{root_path}")
        # else:
        #     print(f"found:{root_path}")
        return mod_info_list

    @staticmethod
    def _find_mod_type(root_path, max_depth=5):
        root = Path(root_path)
        for depth, path in enumerate(root.glob("**/*")):
            path: Path
            if depth > max_depth: break
            if path.name == "maps" and path.is_dir():
                return "Map"
        return "Other"

    def extract_mod(self, file_path):
        """Extract folder names under the mods directory."""
        for _mod_name_dir in os.listdir(file_path):
            # E:\game\SteamLibrary\steamapps\workshop\content\108600\2756689895\mods\InGameMaps\mod.info
            mod_info_list = []
            _mod_info_list = self._find_mod_info(mod_info_list, os.path.join(file_path, _mod_name_dir))
            for _mod_info in _mod_info_list:
                _mod_type = self._find_mod_type(os.path.join(file_path, _mod_name_dir))
                mods_index = _mod_info.parts.index('mods')
                mod_name = _mod_info.parts[mods_index + 1]
                steam_id = _mod_info.parts[mods_index - 1]
                self.parse_file(mod_name, steam_id, _mod_type, _mod_info)

        return self

    def _parse_special_field(self, key, value):
        """Handle special fields (with type conversion)."""
        if key == 'tiledef':
            name, tid = re.split(r'\s+', value, 1)
            return {'name': name.strip(), 'id': int(tid)}
        elif key == 'version':
            return tuple(map(str, value.strip('[]').split(',')))
        return value

    def parse_file(self, mod_name, steam_id, mod_type, mod_info_file_path):
        """Parse the config file and build a nested structure."""
        try:
            self.mod_info.setdefault(mod_name, {})
            mod = self.mod_info[mod_name]
            mod["steam_id"] = steam_id
            mod["mod_type"] = mod_type
            mod["mod_url"] = f"https://steamcommunity.com/sharedfiles/filedetails/?id={steam_id}"
            mod.setdefault("data", [])
            mod_data_list:list = mod["data"]

            with open(mod_info_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                mod_data = {}
                data = defaultdict(list)

                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue

                    key, value = line.split('=', 1)
                    key = key.strip().lower()
                    value = value.strip()

                    parsed_value = self._parse_special_field(key, value)

                    if key in ['description', 'poster', 'pack']:
                        data[key].append(parsed_value)
                    else:
                        data[key]= [parsed_value]

                data['mod_type'] = mod_type
                mod['game_id'] = data['id'][0]

                for k, v in data.items():
                    mod_data[k] = v[0] if len(v) == 1 else v

                mod_data_list.append(mod_data)

                if 'name' in mod_data:
                    self.name_index[mod_data['name']] = mod_name
                self.steam_id_index[steam_id] = mod_name
                self.dir_name_index[mod_data['id']] = mod_name

            return self

        except FileNotFoundError:
            raise ValueError(f"文件不存在: {mod_info_file_path}")

        except Exception as e:
            print(mod_info_file_path)
            raise RuntimeError(f"解析失败: {str(e)}")

    def query_mod(self, identifier):
        """Enhanced multi-key lookup."""
        # Try ID lookup first
        if mod_data := self.mod_info.get(identifier):
            return mod_data

        # Try name lookup
        elif matched_id := self.name_index.get(identifier):
            return self.mod_info[matched_id]

        # Try steam_id lookup
        elif matched_id := self.steam_id_index.get(identifier):
            return self.mod_info[matched_id]

        elif matched_id := self.dir_name_index.get(identifier):
            return self.mod_info[matched_id]
        else:
            # Try folder name lookup
            for mod_id, data in self.mod_info.items():
                if identifier in data:
                    return data[identifier]
        return None


# Usage example
if __name__ == "__main__":
    processor = ModInfoProcessor()
    processor.extract_mod(r"E:\game\SteamLibrary\steamapps\workshop\content\108600\515555911\mods")
    # processor.extract_mod(r"E:\game\SteamLibrary\steamapps\workshop\content\108600\2756689895\mods")
    print(processor.query_mod("MoreBuilds"))
    print(processor.mod_info)
    print(processor.name_index)
    print(processor.steam_id_index)
    print(processor.dir_name_index)
