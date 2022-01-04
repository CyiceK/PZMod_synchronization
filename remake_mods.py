# -*- coding:utf-8 -*-
import configparser
import os


# sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='gb18030')

class PlayerSaveConversionServiceSave:
    def __init__(self):
        self.modname_and_steamid = dict()
        self.modid_and_steamid = dict()
        self.steamid_and_modname = dict()
        self.read_list = []
        self.mods_dict = dict()
        self.ini_data = ""
        self.steam_id = 380870
        self.workshop_path = "F:\Program Files (x86)\Steam\steamapps\common\Project Zomboid Dedicated Server\steamapps\workshop\content\\"
        self.saved_path = "\Zomboid"
        self.my_document = "F:\python\PZ" + self.saved_path
        self.mods_dir = f"{self.workshop_path}{self.steam_id}".replace("\\", "//")
        self.user_save_path = ""

    def read_player_mods(self):
        with open(self.my_document + "\mods\default.txt", "r", encoding="utf-8") as f:
            r_data = f.readlines()
            # print(r_data)
            read_key = None
            start_sw = False
            for dataline in r_data:
                # print(i.replace("\n",""))
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

    def get_modname_and_steamid(self):
        for i in os.listdir(self.mods_dir):
            # print(i)
            if os.path.isdir(self.mods_dir + "//" + i):
                # print(os.listdir(self.mods_dir + "//" + i + "//mods"))
                mods_name = os.listdir(self.mods_dir + "//" + i + "//mods")
                # print(mods_name, " ", i)
                for mod_name in mods_name:
                    self.modname_and_steamid[mod_name] = i
                self.steamid_and_modname[i] = mods_name
            # print(self.modname_and_steamid)
            # print(self.steamid_and_modname)
        return self.modname_and_steamid

    def get_modid_and_steamid(self):
        for i in os.listdir(self.mods_dir):
            # print(i)
            if os.path.isdir(self.mods_dir + "//" + i):
                # print(os.listdir(self.mods_dir + "//" + i + "//mods"))
                mods_name = os.listdir(self.mods_dir + "//" + i + "//mods")
                for mod_name in mods_name:
                    with open(self.mods_dir + "//" + i + "//mods" + "//" + mod_name + "//mod.info", "r",
                              encoding="utf-8") as f:
                        read_list = f.readlines()
                        for j in read_list:
                            j = j.replace("\n", "")
                            j_list = j.split("=")
                            if j_list[0] == "id":
                                # print(j_list)
                                self.modid_and_steamid[j_list[1]] = i
            # print(self.modid_and_steamid)
        return self.modid_and_steamid

    def rw_service_ini(self):
        self.read_player_mods()
        self.get_modid_and_steamid()
        self.get_modname_and_steamid()
        with open(self.my_document + "/Server/servertest.ini", "r") as f:
            self.ini_data = f.readlines()
            for i in self.ini_data:
                i_list = i.split("=")
                if i_list[0] == "Mods":
                    write_Mods_data = ""
                    write_WorkshopItems_data = ""
                    for j in self.mods_dict.get("mods"):
                        if self.modid_and_steamid.get(j) is None:
                            # 缺失的mod
                            # print(self.modname_and_steamid)
                            print(f">同步的MOD名:\033[1;33;47m{j}\033[0m 状态：【\033[5;31;47m缺失\033[0m】<")
                            continue
                        write_Mods_data += j + ";"
                        write_WorkshopItems_data += self.modid_and_steamid.get(j) + ";"
                    # print(ini_data[ini_data.index(i)])
                    # print(write_Mods_data)
                    # print(write_WorkshopItems_data)
                    self.ini_data[self.ini_data.index(i)] = "Mods=" + write_Mods_data[:-1] + "\n"
                elif i_list[0] == "WorkshopItems":
                    self.ini_data[self.ini_data.index(i)] = "WorkshopItems=" + write_WorkshopItems_data[:-1] + "\n"

    def save_ini(self):
        if self.user_save_path == "":
            with open(self.my_document + "/Server/servertest.ini", "w+") as f:
                f.writelines(self.ini_data)
        else:
            with open(self.user_save_path + "/servertest.ini", "w+") as f:
                f.writelines(self.ini_data)
        print(">保存成功！")

    def read_ini(self):
        conf = configparser.ConfigParser()
        conf.read('./PZT.ini')

        self.workshop_path = conf.get("pathconfig", 'workshop_path')
        self.my_document = conf.get("pathconfig", "my_document") + self.saved_path
        self.steam_id = conf.getint("pathconfig", "steam_id")
        self.user_save_path = conf.get("pathconfig", "user_save_path")

        self.mods_dir = f"{self.workshop_path}{self.steam_id}".replace("\\", "//")

    def set_path(self):
        print(f"示例:{self.workshop_path}")
        self.workshop_path = input("输入创意工坊Mod路径：")
        print(f"示例:{self.my_document}")
        self.my_document = input("输入我的文档路径：") + self.saved_path
        print(f"示例:{self.steam_id}")
        self.steam_id = int(input("输入SteamId："))
        print(f"示例:{self.user_save_path}")
        self.user_save_path = input("输入用户保存ini配置的路径：")

    def show_path(self):
        print(f"SteamId:{self.steam_id}")
        print(f"创意工坊Mod路径:{self.workshop_path}")
        print(f"我的文档路径:{self.my_document}")
        print(f"程序组合的路径:{self.mods_dir}")
        print(f"用户保存ini配置的路径:{self.user_save_path}")

    def menu(self):
        while True:
            self.read_ini()
            cmd_list = ["填入路径", "查询路径", "生成到服务器", "退出程序"]
            version = "Ver 0.2"
            print("")
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
                exit(0)


if __name__ == "__main__":
    pz = PlayerSaveConversionServiceSave()
    pz.menu()
