# -*- coding:utf-8 -*-
import configparser
import os


# sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='gb18030')

class PlayerSaveConversionServiceSave:
    def __init__(self):
        self.modname_and_steamid = dict()
        self.modid_and_steamid = dict()
        self.steamid_and_modname = dict()
        self.mods_dict = dict()
        self.read_list = []
        self.ini_data = ""
        self.steam_id = 380870
        self.link_A = ""
        self.link_B = ""
        self.link_document = ""
        self.workshop_path = "F:/Program Files (x86)/Steam/steamapps/common/Project Zomboid Dedicated Server/steamapps/workshop/content//"
        self.saved_path = "/Zomboid"
        self.my_document = "F:/python/PZ" + self.saved_path
        self.mods_dir = f"{self.workshop_path}{self.steam_id}".replace("//", "//")
        self.user_save_path = ""

    def read_player_mods(self):
        with open(self.my_document + "/mods/default.txt", "r", encoding="utf-8") as f:
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
                        write_WorkshopItems_data += self.modid_and_steamid.get(j) + ";"
                    # print(ini_data[ini_data.index(i)])
                    # print(write_Mods_data)
                    # print(write_WorkshopItems_data)
                    self.ini_data[self.ini_data.index(i)] = "Mods=" + write_Mods_data[:-1] + "\n"
                elif i_list[0] == "WorkshopItems":
                    self.ini_data[self.ini_data.index(i)] = "WorkshopItems=" + write_WorkshopItems_data[:-1] + "\n"

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
            with open(self.my_document + "/Server/servertest.ini", "w+") as f:
                f.writelines(self.ini_data)
        else:
            with open(self.user_save_path + "/servertest.ini", "w+") as f:
                f.writelines(self.ini_data)
        print(">保存成功！")

    def read_ini(self):
        conf = configparser.ConfigParser()
        conf.read('./PZT.ini')

        self.workshop_path = conf.get("pathconfig", 'workshop_path') + "//"
        self.my_document = conf.get("pathconfig", "my_document") + self.saved_path + "//"
        self.steam_id = conf.getint("pathconfig", "steam_id")
        self.user_save_path = conf.get("pathconfig", "user_save_path")
        if self.user_save_path != "":
            self.user_save_path += "//"

        self.mods_dir = f"{self.workshop_path}{self.steam_id}".replace("\\", "//")
        self.link_document = conf.get("pathconfig", "link_document")
        self.link_A = conf.get("pathconfig", "link_A") + "//" + self.link_document
        self.link_B = conf.get("pathconfig", "link_B") + "//" + self.link_document

    def set_path(self):
        print(f"示例:{self.workshop_path}")
        self.workshop_path = input("输入创意工坊Mod路径：") + "//"
        print(f"示例:{self.my_document}")
        self.my_document = input("输入我的文档路径：") + self.saved_path + "//"
        print(f"示例:{self.steam_id}")
        self.steam_id = int(input("输入SteamId："))
        print(f"示例:{self.user_save_path}")
        self.user_save_path = input("输入用户保存ini配置的路径：") + "//"

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
            version = "Ver 0.4"
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


if __name__ == "__main__":
    pz = PlayerSaveConversionServiceSave()
    try:
        pz.menu()
    except FileNotFoundError as e:
        print("文件未找到:", e)
        os.system("pause")
    except Exception as e:
        print("致命错误:", e)
        os.system("pause")
