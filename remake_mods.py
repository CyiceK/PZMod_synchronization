# -*- coding:utf-8 -*-
import configparser
import os

# sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='gb18030')
import traceback

from utils.player_archive import PlayerArchive
from utils.steam_client import SteamClient


class PlayerSaveConversionServiceSave(PlayerArchive, SteamClient):
    def __init__(self):
        super().__init__()

    # def web_get_modfile_info(self):
    #     with open('old_data.json', 'r', encoding=self.detect_file_encoding('old_data.json')) as f:
    #         self.modfile_info = json.load(f)
    #
    #     save_list = []
    #     del_list = []
    #     id = 0
    #     for i in self.modfile_info:
    #         modinfo_value = self.modfile_info.get(i)
    #         if modinfo_value.get("poster") is not None:
    #             if len(modinfo_value.get("poster")) != 0:
    #                 pic_dir = modinfo_value.get("poster")[0]
    #         else:
    #             pic_dir = "null"
    #
    #         if modinfo_value.get("require") is not None:
    #             if len(modinfo_value.get("require")) != 0:
    #                 require = modinfo_value.get("require")
    #         else:
    #             require = "null"
    #         data = {
    #             "id": id,
    #             "parent_id": 0,
    #             "order": id,
    #             "pic": f'<img width=100px height=100px src="{pic_dir}"/>',
    #             "modid": i,
    #             "require": require,
    #             "workshopid": f'<a href="https://steamcommunity.com/sharedfiles/filedetails/?id='
    #                           f'{modinfo_value.get("steam_workshop_id")[0]}">{modinfo_value.get("steam_workshop_id")[0]}</a>',
    #             # "children": []
    #         }
    #         save_list.append(data)
    #         id += 1
    #
    #     for i_index in range(len(save_list)):
    #         if save_list[i_index].get("require") != "null":
    #             save_list[i_index]["backgroundColor"] = "#FF0000"
    #             require_len = len(save_list[i_index].get("require"))
    #             count = 0
    #             for j_index in range(len(save_list)):
    #                 for require in save_list[i_index].get("require"):
    #                     if save_list[j_index].get("modid") == require:
    #                         count += 1
    #                         if save_list[i_index]["parent_id"] == 0:
    #                             save_list[i_index]["parent_id"] = save_list[j_index].get("id")
    #                             save_list[i_index]["backgroundColor"] = "#00FF99"
    #                             save_list[j_index].setdefault("children", [])
    #                             save_list[j_index].get("children").append(save_list[i_index])
    #                             del_list.append(i_index)
    #                             # 表格不给父子同级啊（恼
    #                             continue
    #             if count != require_len:
    #                 print("红了",count," ",require_len)
    #                 print(save_list[i_index].get("require"))
    #                 save_list[i_index]["backgroundColor"] = "#FF0000"
    #
    #     for del_index in del_list:
    #         save_list[del_index] = None
    #
    #     i = 0
    #     while i < len(save_list):
    #         if save_list[i] is None:
    #             save_list.pop(i)
    #             i -= 1
    #         else:
    #             # print(save_list[i])
    #             pass
    #
    #         i += 1
    #
    #     with open('data.json', 'w', encoding=self.detect_file_encoding()) as f:
    #         json.dump(save_list, f, indent=4)

    def rw_service_ini(self):
        self.read_player_mods()
        self.get_modid_and_steamid()
        self.get_modname_and_steamid()
        with open(f"{self.my_document}/Server/servertest.ini", "r", encoding=self.detect_file_encoding(f"{self.my_document}/Server/servertest.ini")) as f:
            self.ini_data = f.readlines()
            for i in self.ini_data:
                i_list = i.split("=")
                if i_list[0] == "Mods":
                    write_Mods_data = ""
                    write_WorkshopItems_data = ""
                    writed_WorkshopItems_data = []
                    print("===按模块启用顺序排列===")
                    print("启用顺序是根据您模块点击开启排序，先开先启用原则")
                    for j in self.mods_dict.get("mods"):
                        if self.modid_and_steamid.get(j) is None:
                            # 缺失的mod
                            # print(self.modname_and_steamid)
                            print(
                                f">同步的MOD名:\033[1;31;40m{j:<30}\t\033[0mID:\033[1;31;40m{self.modid_and_steamid.get(j)}\t\033[0m状态：【\033[5;31;47m缺失\033[0m】<")
                            continue
                        else:
                            print(
                                f">同步的MOD名:\033[0;32;40m{j:<30}\t\033[0mID:\033[1;33;40m{self.modid_and_steamid.get(j)}\t\033[0m状态：【\033[0;32;47m成功\033[0m】<")
                        write_Mods_data += j + ";"
                        if not self.modid_and_steamid.get(j) in writed_WorkshopItems_data:
                            write_WorkshopItems_data += self.modid_and_steamid.get(j) + ";"
                            writed_WorkshopItems_data.append(self.modid_and_steamid.get(j))
                    # print(ini_data[ini_data.index(i)])
                    # print(write_Mods_data)
                    # print(write_WorkshopItems_data)
                    self.ini_data[self.ini_data.index(i)] = f"Mods={write_Mods_data[:-1]}\n"
                elif i_list[0] == "WorkshopItems":
                    self.ini_data[self.ini_data.index(i)] = f"WorkshopItems={write_WorkshopItems_data[:-1]}\n"

    def build_soft_link(self):
        self.read_ini()
        print("A_B目录下的结构推荐要一致")
        print("1.Windows 2.Linux 3.Mac[不支持]")
        user_chose = input("选择你的操作系统类型:")
        if user_chose == "1":
            # print(f'mklink /J "{self.link_A}" "{self.link_B}"')
            os.system(f'mklink /J "{self.link_A}" "{self.link_B}"')
        elif user_chose == "2":
            # print(f'ln -sT "{self.link_B}" "{self.link_A}"')
            os.system(f'ln -sT "{self.link_B}" "{self.link_A}"')
        else:
            print(">所选系统不支持")

    def save_ini(self):
        if self.user_save_path == "":
            with open(self.my_document + "/Server/servertest.ini", "w+", encoding="utf-8") as f:
                f.writelines(self.ini_data)
        else:
            with open(self.user_save_path + "/servertest.ini", "w+", encoding="utf-8") as f:
                f.writelines(self.ini_data)
        print(">保存成功！")

    def read_ini(self):
        conf = configparser.ConfigParser()
        conf.read('./PZT.ini')

        self.workshop_path = f"{conf.get('pathconfig', 'workshop_path')}//"
        if conf.get('pathconfig', 'my_document'):
            f"{conf.get('pathconfig', 'my_document')}{self.player_archive_path}//"
        else:
            self.my_document = f"{self.get_my_documents_path()}{self.player_archive_path}//"
        self.steam_id = conf.getint("pathconfig", "steam_id")
        self.user_save_path = conf.get("pathconfig", "user_save_path")
        if self.user_save_path != "":
            self.user_save_path += "//"

        self.mods_dir = f"{self.workshop_path}{self.steam_id}".replace("\\", "//")
        self.link_document = conf.get("pathconfig", "link_document")
        self.link_A = f"{conf.get('pathconfig', 'link_A')}//{self.link_document}"
        self.link_B = f"{conf.get('pathconfig', 'link_B')}//{self.link_document}"

    def set_path(self):
        print(f"示例:{self.workshop_path}")
        self.workshop_path = f"{input('输入创意工坊Mod路径：')}//"
        print(f"示例:{self.my_document}")
        self.my_document = f"{input('输入我的文档路径：')}{self.player_archive_path}//"
        print(f"示例:{self.steam_id}")
        self.steam_id = int(input("输入SteamId："))
        print(f"示例:{self.user_save_path}")
        self.user_save_path = f"{input('输入用户保存ini配置的路径：')}//"

    def show_path(self):
        self.read_ini()
        print(f"SteamId:{self.steam_id}")
        print(f"创意工坊Mod路径:{self.workshop_path}")
        print(f"我的文档路径:{self.my_document}")
        print(f"程序组合的路径:{self.mods_dir}")
        print(f"用户保存ini配置的路径:{self.user_save_path}")

    def menu(self):
        while True:
            self.read_ini()
            cmd_list = ["填入临时路径", "查询路径", "生成到服务器", "建立链接", "退出程序"]
            version = "Ver 0.5"
            print(" ")
            print(f"========存档MOD与服务器MOD同步工具{version}==========")
            print("========Write By:CyiceK==========")
            for i in range(0, len(cmd_list)):
                print(f"||\t{i + 1}.{cmd_list[i]}\t||")
            print("===============================")

            user_input = input(">>输入命令序号：")

            if user_input == "1":
                self.set_path()
            elif user_input == "2":
                self.show_path()
            elif user_input == "3":
                self.rw_service_ini()
                user_input = input(">>是否保存？【Y/N(任意)】")
                if user_input == "Y":
                    self.save_ini()
                else:
                    print(">不保存")
                    continue
            elif user_input == "4":
                self.build_soft_link()
            elif user_input == "5":
                exit(0)

    # def get_info_api(self):
    #     # self.read_ini()
    #     # self.read_player_mods()
    #     # self.get_modid_and_steamid()
    #     # self.get_modname_and_steamid()
    #     # print(self.modid_and_steamid)
    #     # self.get_modfile_info()
    #     self.web_get_modfile_info()


if __name__ == "__main__":
    pz = PlayerSaveConversionServiceSave()
    try:
        pz.menu()
        # pz.get_info_api()
    except FileNotFoundError as e:
        print("文件未找到:", e)
        os.system("pause")
    except Exception as e:
        traceback.print_exc()
        print("致命错误:", e)
        os.system("pause")
