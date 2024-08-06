# -*- coding:utf-8 -*-
import concurrent.futures
import json
import os
import shutil

from tools.tools import Tools


class SteamClient(Tools):
    def __init__(self):
        super().__init__()
        self.steam_id = 380870
        self.workshop_path = "F:/Program Files (x86)/Steam/steamapps/common/Project Zomboid Dedicated Server/steamapps/workshop/content//"
        self.mods_dir = f"{self.workshop_path}{self.steam_id}".replace("//", "//")
        self.modname_and_steamid = dict()
        self.modid_and_steamid = dict()
        self.steamid_and_modname = dict()
        self.modfile_info = dict()

    def get_modname_and_steamid(self) -> dict:
        def process_mods(i):
            if os.path.isdir(f"{self.mods_dir}//{i}"):
                mods_name = os.listdir(f"{self.mods_dir}//{i}//mods")
                for mod_name in mods_name:
                    self.modname_and_steamid[mod_name] = i
                self.steamid_and_modname[i] = mods_name

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_mods, i): i for i in os.listdir(self.mods_dir)}
            for future in concurrent.futures.as_completed(futures):
                future.result()

        return self.modname_and_steamid

    def get_modid_and_steamid(self) -> dict:
        def process_mod_info(i):
            if os.path.isdir(self.mods_dir + "//" + i):
                mods_name = os.listdir(self.mods_dir + "//" + i + "//mods")
                for mod_name in mods_name:
                    try:
                        with open(f"{self.mods_dir}//{i}//mods//{mod_name}//mod.info", "r",
                                  encoding=self.detect_file_encoding(
                                      f"{self.mods_dir}//{i}//mods//{mod_name}//mod.info")) as f:
                            read_list = f.readlines()
                            for j in read_list:
                                j = j.replace("\n", "")
                                j_list = j.split("=")
                                if j_list[0] == "id":
                                    self.modid_and_steamid[j_list[1]] = i
                    except FileNotFoundError:
                        continue

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_mod_info, i) for i in os.listdir(self.mods_dir)]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        return self.modid_and_steamid

    def get_modfile_info(self):
        def process_mod_info(i):
            id_key = "init"
            if os.path.isdir(f"{self.mods_dir}//{i}"):
                mods_name = os.listdir(f"{self.mods_dir}//{i}//mods")
                for mod_name in mods_name:
                    try:
                        with open(f"{self.mods_dir}//{i}//mods//{mod_name}//mod.info", "r",
                                  encoding=f"{self.mods_dir}//{i}//mods//{mod_name}//mod.info") as f:
                            read_list = f.readlines()
                            for mod_file_text in read_list:
                                mod_file_text_list = mod_file_text.replace("\n", "").split("=")
                                if mod_file_text_list[0] == "id":
                                    id_key = mod_file_text_list[1]
                                    self.modfile_info[id_key] = {}
                                    self.modfile_info[id_key].setdefault("steam_workshop_id",
                                                                         [self.modid_and_steamid.get(id_key)])

                                for mod_file_text in read_list:
                                    mod_file_text = mod_file_text.replace("\n", "")
                                    mod_file_text_list = mod_file_text.split("=")

                                    if len(mod_file_text_list) < 2 or id_key == "init":
                                        continue

                                    if mod_file_text_list[0] == "poster":
                                        if mod_file_text_list[1] != "":
                                            if not os.path.exists(f"./img/{self.modid_and_steamid.get(id_key)}"):
                                                os.mkdir(f"./img/{self.modid_and_steamid.get(id_key)}")
                                            if not os.path.exists(
                                                    f"./img/{self.modid_and_steamid.get(id_key)}//{mod_name}"):
                                                os.mkdir(f"./img/{self.modid_and_steamid.get(id_key)}//{mod_name}")
                                            if not os.path.exists(
                                                    f"./img/{self.modid_and_steamid.get(id_key)}//{mod_name}//{mod_file_text_list[1]}"):
                                                shutil.copy(
                                                    f"{self.mods_dir}//{i}//mods//{mod_name}//{mod_file_text_list[1]}",
                                                    f"./img/{self.modid_and_steamid.get(id_key)}//{mod_name}")
                                                mod_file_text_list[
                                                    1] = f"./img/{self.modid_and_steamid.get(id_key)}//{mod_name}//{mod_file_text_list[1]}"

                                    if mod_file_text_list[0] == "require":
                                        mod_file_text_list[1] = mod_file_text_list[1].split(',')

                                    self.modfile_info[id_key].setdefault(mod_file_text_list[0], [])
                                    if isinstance(mod_file_text_list[1], list):
                                        for tmp_data in mod_file_text_list[1]:
                                            self.modfile_info[id_key][mod_file_text_list[0]].append(tmp_data)
                                    else:
                                        self.modfile_info[id_key][mod_file_text_list[0]].append(mod_file_text_list[1])
                    except FileNotFoundError:
                        continue

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_mod_info, i) for i in os.listdir(self.mods_dir)]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        # print(self.modfile_info)
        with open('old_data.json', 'w', encoding=self.detect_file_encoding('old_data.json')) as f:
            json.dump(self.modfile_info, f, indent=4)
