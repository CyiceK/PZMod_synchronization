# -*- coding:utf-8 -*-
import os

from tools.tools import Tools


class PlayerArchive(Tools):
    def __init__(self):
        super().__init__()
        self.player_archive_path = "/Zomboid"
        self.my_document = f"F:/python/PZ{self.player_archive_path}"

    def read_player_mods(self):
        with open(f"{self.my_document}/mods/default.txt", "r",
                  encoding=self.detect_file_encoding(f"{self.my_document}/mods/default.txt")) as f:
            r_data = f.readlines()
            # print(r_data)
            read_key = None
            start_sw = False
            for dataline in r_data:
                # print(i.replace("/n",""))
                dataline = dataline.replace("\n", "").replace("\t", "")
                # 识别版本
                if dataline.split("=")[0] == "VERSION ":
                    self.mods_dict["VERSION"] = dataline.split("=")[1].replace(",", "")

                if dataline == "{":
                    start_sw = True
                    continue
                elif dataline == "}":
                    start_sw = False
                    continue
                if start_sw:
                    # i.split("=") 这是以 = 分家
                    self.read_list.append(dataline.split("=")[-1][1:-1])
                    self.mods_dict[read_key] = self.read_list
                else:
                    read_key = dataline.split("=")[0]
                    if len(dataline) > 0:
                        # print(read_key, " ", type(read_key), " ", len(read_key))
                        # print(self.mods_dict)
                        self.read_list = []
            # print(mods_dict)

    @staticmethod
    def get_my_documents_path() -> str:
        user_dir = os.path.expanduser('~')
        documents_dir = os.path.join(user_dir, "Documents").replace("\\", "//")
        return documents_dir
