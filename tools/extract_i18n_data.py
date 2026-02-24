#!/usr/bin/env python3
"""
从游戏文件提取 i18n 翻译数据并生成 JSON 文件。
"""
import json
import re
from pathlib import Path
from datetime import date


def parse_item_name_file(file_path: Path) -> dict[str, str]:
    """解析 ItemName_CN.txt 文件，返回 {key: value} 字典。"""
    translations = {}
    if not file_path.exists():
        return translations
    
    content = file_path.read_text(encoding='utf-8')
    # 匹配格式：ItemName_模块。物品 = "中文名称"
    pattern = r'ItemName_(\w+)\.([\w.]+)\s*=\s*"([^"]*)"'
    
    for match in re.finditer(pattern, content):
        module, item_key, translation = match.groups()
        full_key = f"{module}.{item_key}"
        translations[full_key] = translation
    
    return translations


def parse_recorded_media_file(file_path: Path) -> dict[str, str]:
    """解析 Recorded_Media_CN.txt 文件，返回 {key: value} 字典。"""
    translations = {}
    if not file_path.exists():
        return translations
    
    content = file_path.read_text(encoding='utf-8')
    # 匹配格式：RM_标识 = "中文名称"
    pattern = r'^(RM_[\w-]+)\s*=\s*"([^"]*)"'
    
    for match in re.finditer(pattern, content, re.MULTILINE):
        key, translation = match.groups()
        translations[key] = translation
    
    return translations


def compare_versions(b41_data: dict, b42_data: dict) -> tuple[dict, dict]:
    """比较 b41 和 b42 数据，返回共同数据和版本差异。"""
    common = {}
    b42_only = {}
    
    for key, value in b41_data.items():
        if key in b42_data:
            if b42_data[key] == value:
                common[key] = value
            else:
                # 版本差异
                common[key] = value
                b42_only[key] = b42_data[key]
        else:
            common[key] = value
    
    # b42 新增的键
    for key, value in b42_data.items():
        if key not in b41_data:
            b42_only[key] = value
    
    return common, b42_only


def generate_items_json(b41_path: Path, b42_path: Path, output_path: Path) -> None:
    """生成物品翻译 JSON 文件。"""
    print(f"解析 b41 物品文件：{b41_path}")
    b41_data = parse_item_name_file(b41_path)
    print(f"  找到 {len(b41_data)} 条翻译")
    
    print(f"解析 b42 物品文件：{b42_path}")
    b42_data = parse_item_name_file(b42_path)
    print(f"  找到 {len(b42_data)} 条翻译")
    
    common, b42_diff = compare_versions(b41_data, b42_data)
    print(f"  共同翻译：{len(common)} 条")
    print(f"  b42 差异/新增：{len(b42_diff)} 条")
    
    # 构建 JSON 结构
    output_data = {
        "_meta": {
            "category": "game_items",
            "language": "zh_CN",
            "version": "1.0.0",
            "source": "extracted",
            "description": "游戏物品翻译 - 从游戏文件提取",
            "game_version": {
                "b41": "41.78.16",
                "b42": "42.0.0"
            },
            "last_updated": str(date.today()),
            "entry_count": len(common)
        },
        "translations": dict(sorted(common.items())),
        "version_overrides": {},
        "mod_overrides": {}
    }
    
    # 如果有 b42 差异，添加到 version_overrides
    if b42_diff:
        output_data["version_overrides"]["b42"] = dict(sorted(b42_diff.items()))
    
    # 写入文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"已写入：{output_path}")


def generate_media_json(b41_path: Path, b42_path: Path, output_path: Path) -> None:
    """生成媒体翻译 JSON 文件。"""
    print(f"解析 b41 媒体文件：{b41_path}")
    b41_data = parse_recorded_media_file(b41_path)
    print(f"  找到 {len(b41_data)} 条翻译")
    
    print(f"解析 b42 媒体文件：{b42_path}")
    b42_data = parse_recorded_media_file(b42_path)
    print(f"  找到 {len(b42_data)} 条翻译")
    
    common, b42_diff = compare_versions(b41_data, b42_data)
    print(f"  共同翻译：{len(common)} 条")
    print(f"  b42 差异/新增：{len(b42_diff)} 条")
    
    output_data = {
        "_meta": {
            "category": "game_media",
            "language": "zh_CN",
            "version": "1.0.0",
            "source": "extracted",
            "description": "媒体记录翻译 - VHS 录像带/CD 等",
            "game_version": {
                "b41": "41.78.16",
                "b42": "42.0.0"
            },
            "last_updated": str(date.today()),
            "entry_count": len(common)
        },
        "translations": dict(sorted(common.items())),
        "version_overrides": {},
        "mod_overrides": {}
    }
    
    if b42_diff:
        output_data["version_overrides"]["b42"] = dict(sorted(b42_diff.items()))
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"已写入：{output_path}")


def generate_player_json(output_path: Path) -> None:
    """生成玩家数据翻译 JSON 文件（基于 player_blob_parser.py 中的字段）。"""
    # 身体部位（17 个）
    body_parts = {
        "Head": "头部",
        "Neck": "颈部",
        "Torso_Upper": "胸部",
        "Torso_Lower": "腹部",
        "UpperArm_L": "左上臂",
        "UpperArm_R": "右上臂",
        "ForeArm_L": "左前臂",
        "ForeArm_R": "右前臂",
        "Hand_L": "左手",
        "Hand_R": "右手",
        "Groin": "腹股沟",
        "UpperLeg_L": "左大腿",
        "UpperLeg_R": "右大腿",
        "LowerLeg_L": "左小腿",
        "LowerLeg_R": "右小腿",
        "Foot_L": "左脚",
        "Foot_R": "右脚"
    }
    
    # 技能
    skills = {
        "Fitness": "体能",
        "Strength": "力量",
        "Sprinting": "冲刺",
        "Lightfoot": "轻步",
        "Nimble": "灵巧",
        "Sneak": "潜行",
        "Aiming": "瞄准",
        "Reloading": "装填",
        "LongBlade": "长刃",
        "SmallBlade": "短刃",
        "Axe": "斧类",
        "Blunt": "钝器",
        "SmallBlunt": "小钝器",
        "LongBlunt": "长棍",
        "ShortBlunt": "短棍",
        "ShortBlade": "短刃",
        "Spear": "长矛",
        "Maintenance": "维护",
        "Carpentry": "木工",
        "Electricity": "电工",
        "MetalWelding": "金工",
        "Mechanics": "技工",
        "Tailoring": "缝纫",
        "Cooking": "烹饪",
        "Farming": "农业",
        "Fishing": "钓鱼",
        "Trapping": "捕猎",
        "PlantScavenging": "觅食",
        "Foraging": "觅食",
        "FirstAid": "急救"
    }
    
    # 技能分组
    skill_groups = {
        "passive": "被动",
        "agility": "敏捷",
        "combat": "战斗",
        "firearm": "枪械",
        "crafting": "制作",
        "survival": "生存",
        "other": "其他"
    }
    
    # 特性
    traits = {
        "Brave": "勇敢",
        "Cowardly": "胆小",
        "FastLearner": "快速学习者",
        "SlowLearner": "慢速学习者",
        "Athletic": "运动健将",
        "OutOfShape": "体型走样",
        "Strong": "强壮",
        "Weak": "虚弱",
        "FastHealer": "快速恢复",
        "SlowHealer": "缓慢恢复",
        "ThickSkinned": "厚皮",
        "ThinSkinned": "薄皮",
        "NightVision": "夜视",
        "EagleEyed": "鹰眼",
        "ShortSighted": "近视",
        "Deaf": "失聪",
        "HardOfHearing": "听力困难",
        "KeenHearing": "敏锐听觉",
        "Graceful": "优雅",
        "Clumsy": "笨拙",
        "Organized": "有条理",
        "Disorganized": "无条理",
        "Lucky": "幸运",
        "Unlucky": "不幸",
        "SpeedDemon": "速度狂魔",
        "SundayDriver": "周日司机",
        "Constructor": "建筑师",
        "Handy": "手巧",
        "FirstAidTraining": "急救训练",
        "FishingTraining": "钓鱼训练",
        "PlantAffinity": "植物亲和",
        "CookingTraining": "烹饪训练",
        "TrappingTraining": "捕猎训练"
    }
    
    # Moodles（状态）
    moodles = {
        "Hungry": "饥饿",
        "Thirsty": "口渴",
        "Fatigue": "疲劳",
        "Stress": "压力",
        "Pain": "疼痛",
        "Sick": "恶心",
        "Bored": "无聊",
        "Unhappy": "不快",
        "Wet": "潮湿",
        "Cold": "寒冷",
        "Hot": "炎热",
        "Windchill": "风寒",
        "HasACold": "感冒",
        "Injured": "受伤",
        "Bleeding": "流血",
        "Hyperthermia": "中暑",
        "Hypothermia": "失温",
        "Drunk": "醉酒",
        "FoodSick": "食物中毒",
        "Dead": "死亡"
    }
    
    # 状态（stats）
    stats = {
        "anger": "愤怒",
        "boredom": "无聊",
        "endurance": "耐力",
        "fatigue": "疲劳",
        "fitness": "体能",
        "hunger": "饥饿",
        "morale": "士气",
        "stress": "压力",
        "fear": "恐惧",
        "panic": "恐慌",
        "sanity": "理智",
        "sickness": "疾病",
        "boredom_level": "无聊等级",
        "pain": "疼痛",
        "drunkenness": "醉酒度",
        "thirst": "口渴",
        "stress_from_cigarettes": "香烟压力"
    }
    
    # 职业
    professions = {
        "none": "无业",
        "hobo": "流浪汉",
        "student": "学生",
        "unemployed": "失业",
        "burglar": "窃贼",
        "chef": "厨师",
        "constructionworker": "建筑工人",
        "farmer": "农民",
        "factoryworker": "工厂工人",
        "fireofficer": "消防官",
        "fitnessinstructor": "健身教练",
        "doctor": "医生",
        "nurse": "护士",
        "paramedic": "护理人员",
        "engineer": "工程师",
        "mechanic": "技工",
        "maintenance": "维修工",
        "electrician": "电工",
        "plumber": "管道工",
        "carpenter": "木匠",
        "metalworker": "金工",
        "welder": "焊工",
        "veteran": "老兵",
        "policeofficer": "警察",
        "securityguard": "保安",
        "prisonofficer": "狱警",
        "ranger": "护林员",
        "fireman": "消防员",
        "scientist": "科学家",
        "teacher": "教师",
        "journalist": "记者",
        "programmer": "程序员",
        "artist": "艺术家",
        "musician": "音乐家",
        "writer": "作家",
        "actor": "演员",
        "lawyer": "律师",
        "psychologist": "心理学家",
        "pilot": "飞行员",
        "soldier": "士兵",
        "magician": "魔术师",
        "chef_head": "主厨",
        "chef_sous": "副厨",
        "chef_pastry": "糕点师",
        "chef_grill": "烧烤师",
        "chef_fry": "油炸师",
        "chef_salad": "沙拉师",
        "chef_butcher": "屠夫",
        "chef_baker": "面包师"
    }
    
    output_data = {
        "_meta": {
            "category": "archive_player",
            "language": "zh_CN",
            "version": "1.0.0",
            "source": "manual",
            "description": "玩家数据翻译 - 身体部位、状态、技能、特性",
            "game_version": {
                "b41": "41.78.16",
                "b42": "42.0.0"
            },
            "last_updated": str(date.today()),
            "entry_count": len(body_parts) + len(skills) + len(traits) + len(moodles) + len(stats) + len(professions)
        },
        "body_parts": body_parts,
        "skills": skills,
        "skill_groups": skill_groups,
        "traits": traits,
        "moodles": moodles,
        "stats": stats,
        "professions": professions,
        "version_overrides": {},
        "mod_overrides": {}
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"已写入：{output_path}")


def generate_vehicle_json(output_path: Path) -> None:
    """生成载具数据翻译 JSON 文件。"""
    # 载具部件
    parts = {
        "Engine": "引擎",
        "Battery": "电池",
        "GasTank": "油箱",
        "TireFrontLeft": "前左轮胎",
        "TireFrontRight": "前右轮胎",
        "TireRearLeft": "后左轮胎",
        "TireRearRight": "后右轮胎",
        "BrakeFrontLeft": "前左刹车",
        "BrakeFrontRight": "前右刹车",
        "BrakeRearLeft": "后左刹车",
        "BrakeRearRight": "后右刹车",
        "SuspensionFrontLeft": "前左悬挂",
        "SuspensionFrontRight": "前右悬挂",
        "SuspensionRearLeft": "后左悬挂",
        "SuspensionRearRight": "后右悬挂",
        "DoorFrontLeft": "前左车门",
        "DoorFrontRight": "前右车门",
        "DoorRearLeft": "后左车门",
        "DoorRearRight": "后右车门",
        "WindowFrontLeft": "前左车窗",
        "WindowFrontRight": "前右车窗",
        "WindowRearLeft": "后左车窗",
        "WindowRearRight": "后右车窗",
        "Windshield": "挡风玻璃",
        "WindshieldRear": "后挡风玻璃",
        "HeadlightLeft": "左前大灯",
        "HeadlightRight": "右前大灯",
        "HeadlightRearLeft": "左后尾灯",
        "HeadlightRearRight": "右后尾灯",
        "SeatFrontLeft": "前左座椅",
        "SeatFrontRight": "前右座椅",
        "SeatRearLeft": "后左座椅",
        "SeatRearRight": "后右座椅",
        "GloveBox": "手套箱",
        "TruckBed": "货箱",
        "Radio": "收音机",
        "Heater": "加热器",
        "Muffler": "消声器",
        "Alternator": "交流发电机",
        "Radiator": "散热器",
        "WaterPump": "水泵",
        "FuelPump": "燃油泵",
        "FuelLine": "燃油管",
        "BrakeLine": "刹车管",
        "Clutch": "离合器",
        "Transmission": "变速器",
        "DriveShaft": "传动轴",
        "Differential": "差速器",
        "AxleFront": "前轴",
        "AxleRear": "后轴",
        "WheelFrontLeft": "前左车轮",
        "WheelFrontRight": "前右车轮",
        "WheelRearLeft": "后左车轮",
        "WheelRearRight": "后右车轮",
        "HubCap": "轮毂盖",
        "Rim": "轮圈",
        "MirrorLeft": "左后视镜",
        "MirrorRight": "右后视镜",
        "WiperFront": "前雨刷",
        "WiperRear": "后雨刷",
        "Horn": "喇叭",
        "AirFilter": "空气滤清器",
        "SparkPlug": "火花塞",
        "Ignition": "点火系统",
        "Starter": "起动机",
        "CatalyticConverter": "催化转化器",
        "ExhaustPipe": "排气管",
        "Tailpipe": "尾管",
        "BumperFront": "前保险杠",
        "BumperRear": "后保险杠",
        "Hood": "发动机罩",
        "Trunk": "后备箱",
        "FenderFrontLeft": "前左翼子板",
        "FenderFrontRight": "前右翼子板",
        "QuarterPanelLeft": "左后侧围板",
        "QuarterPanelRight": "右后侧围板",
        "Roof": "车顶",
        "Sunroof": "天窗",
        "Antenna": "天线",
        "LicensePlate": "车牌",
        "Emblem": "车标",
        "Lightbar": "警灯条",
        "Siren": "警报器"
    }
    
    # 部件分类
    part_categories = {
        "engine": "引擎与设备",
        "doors": "车门",
        "windows": "车窗",
        "lights": "灯光",
        "storage": "储物",
        "wheels": "车轮",
        "body": "车身",
        "electrical": "电气",
        "exhaust": "排气",
        "other": "其他零件"
    }
    
    # 载具类型
    types = {
        "Base.CarNormal": "普通轿车",
        "Base.CarSports": "跑车",
        "Base.CarLuxury": "豪华轿车",
        "Base.CarTaxi": "出租车",
        "Base.CarTaxi2": "出租车 2",
        "Base.PickUpTruck": "皮卡",
        "Base.PickUpVan": "厢式皮卡",
        "Base.PickUpTruckMccoy": "麦考伊皮卡",
        "Base.Van": "厢式货车",
        "Base.VanSeats": "座式货车",
        "Base.VanAmbulance": "救护车",
        "Base.VanRadio": "广播车",
        "Base.VanSpiffo": "斯皮福货车",
        "Base.Van_KnoxDisti": "诺克斯货车",
        "Base.Van_LectroMax": "莱克特罗货车",
        "Base.Van_MassGenFac": "大众货车",
        "Base.Van_Transit": "运输货车",
        "Base.StepVan": "步梯货车",
        "Base.StepVan_Heralds": "先驱货车",
        "Base.StepVanMail": "邮车",
        "Base.StepVan_Scarlet": "斯卡利特货车",
        "Base.SUV": "SUV",
        "Base.OffRoad": "越野车",
        "Base.SmallCar": "小型车",
        "Base.SmallCar02": "小型车 2",
        "Base.ModernCar": "现代轿车",
        "Base.ModernCar02": "现代轿车 2",
        "Base.SportsCar": "超级跑车",
        "Base.Ambulance": "救护车",
        "Base.Fire": "消防车",
        "Base.Police": "警车",
        "Base.Ranger": "护林员车",
        "Base.Cargo": "货运卡车",
        "Base.VanSpecial": "特种货车",
        "Base.Trailer": "拖车",
        "Base.TrailerCamping": "露营拖车",
        "Base.TrailerCargo": "货运拖车",
        "Base.Bus": "巴士",
        "Base.BusSchool": "校车",
        "Base.BusCity": "城市巴士",
        "Base.Magazine": "杂志车",
        "Base.GolfCart": "高尔夫球车",
        "Base.Tractor": "拖拉机",
        "Base.Bike": "自行车",
        "Base.Motorcycle": "摩托车",
        "Base.Motorbike": "摩托车",
        "Base.Scooter": "踏板车"
    }
    
    output_data = {
        "_meta": {
            "category": "archive_vehicle",
            "language": "zh_CN",
            "version": "1.0.0",
            "source": "manual",
            "description": "载具数据翻译 - 部件、类型",
            "game_version": {
                "b41": "41.78.16",
                "b42": "42.0.0"
            },
            "last_updated": str(date.today()),
            "entry_count": len(parts) + len(types)
        },
        "parts": parts,
        "part_categories": part_categories,
        "types": types,
        "version_overrides": {},
        "mod_overrides": {}
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"已写入：{output_path}")


def generate_content_json(output_path: Path) -> None:
    """生成区块内容翻译 JSON 文件。"""
    # 容器类型
    containers = {
        "fridge": "冰箱",
        "freezer": "冷冻柜",
        "stove": "炉灶",
        "microwave": "微波炉",
        "counter": "柜台",
        "crate": "木箱",
        "cardboardbox": "纸箱",
        "bin": "垃圾桶",
        "officedrawers": "办公抽屉",
        "filingcabinet": "文件柜",
        "wardrobe": "衣柜",
        "dresser": "梳妆台",
        "sidetable": "床头柜",
        "coffeetable": "茶几",
        "shelves": "货架",
        "metal_shelves": "金属货架",
        "locker": "储物柜",
        "toolcabinet": "工具柜",
        "medicine": "药柜",
        "firstaid": "急救箱",
        "dishescabinet": "碗柜",
        "overhead": "吊柜",
        "bookcase": "书架",
        "shelvesmag": "杂志架",
        "countertop": "台面",
        "kitchen_cabinet": "橱柜",
        "bathroom_cabinet": "浴室柜",
        "garage_storage": "车库储物",
        "furnace": "熔炉",
        "fireplace": "壁炉",
        "barbecue": "烧烤架",
        "composter": "堆肥箱",
        "rainbarrel": "雨水桶",
        "agricultural_shelf": "农业架",
        "safe": "保险箱",
        "displaycase": "展示柜",
        "trashcan": "垃圾桶",
        "dumpster": "垃圾箱",
        "cooler": "冷藏箱",
        "barrel": "桶",
        "pot": "锅",
        "pan": "平底锅",
        "bowl": "碗",
        "plate": "盘子",
        "cup": "杯子",
        "bottle": "瓶子",
        "can": "罐头",
        "box": "盒子",
        "bag": "袋子",
        "backpack": "背包",
        "suitcase": "手提箱",
        "purse": "手提包",
        "wallet": "钱包",
        "pocket": "口袋",
        "holster": "枪套",
        "quiver": "箭袋",
        "ammo_box": "弹药箱",
        "grenade_box": "手雷箱",
        "medkit": "医疗包",
        "toolbox": "工具箱",
        "tackle_box": "渔具盒",
        "seed_bag": "种子袋",
        "fertilizer_bag": "肥料袋",
        "pet_food": "宠物食品",
        "bird_feeder": "喂鸟器",
        "beehive": "蜂箱",
        "trap": "陷阱",
        "cage": "笼子",
        "aquarium": "鱼缸",
        "terrarium": "生态箱",
        "planter": "花盆",
        "vase": "花瓶",
        "urn": "骨灰盒",
        "coffin": "棺材",
        "grave": "坟墓",
        "tomb": "墓穴",
        "crypt": "地穴",
        "sarcophagus": "石棺"
    }
    
    # 建筑类型
    buildings = {
        "house": "住宅",
        "store": "商店",
        "warehouse": "仓库",
        "gasstation": "加油站",
        "restaurant": "餐厅",
        "hospital": "医院",
        "pharmacy": "药店",
        "policestation": "警察局",
        "firestation": "消防局",
        "school": "学校",
        "church": "教堂",
        "motel": "汽车旅馆",
        "bar": "酒吧",
        "gym": "健身房",
        "library": "图书馆",
        "bank": "银行",
        "construction": "建筑工地",
        "factory": "工厂",
        "farm": "农场",
        "cabin": "小屋",
        "barn": "谷仓",
        "shed": "棚屋",
        "garage": "车库",
        "carport": "车棚",
        "greenhouse": "温室",
        "silo": "筒仓",
        "windmill": "风车",
        "watertower": "水塔",
        "tower": "塔",
        "antenna": "天线塔",
        "lighthouse": "灯塔",
        "pier": "码头",
        "dock": "船坞",
        "marina": "游艇码头",
        "hangar": "机库",
        "airport": "机场",
        "heliport": "直升机坪",
        "trainstation": "火车站",
        "subway": "地铁站",
        "busstation": "公交车站",
        "parking": "停车场",
        "park": "公园",
        "playground": "游乐场",
        "zoo": "动物园",
        "museum": "博物馆",
        "theater": "剧院",
        "cinema": "电影院",
        "stadium": "体育场",
        "arena": "竞技场",
        "courthouse": "法院",
        "cityhall": "市政厅",
        "embassy": "大使馆",
        "prison": "监狱",
        "militarybase": "军事基地",
        "bunker": "地堡",
        "laboratory": "实验室",
        "research": "研究中心",
        "office": "办公楼",
        "skyscraper": "摩天大楼",
        "apartment": "公寓",
        "condo": "公寓楼",
        "dormitory": "宿舍",
        "orphanage": "孤儿院",
        "nursinghome": "养老院",
        "hospice": "临终关怀院",
        "clinic": "诊所",
        "dentist": "牙科诊所",
        "veterinarian": "兽医诊所",
        "petstore": "宠物店",
        "grocery": "杂货店",
        "supermarket": "超市",
        "convenience": "便利店",
        "bakery": "面包店",
        "butcher": "肉店",
        "fishmarket": "鱼市",
        "florist": "花店",
        "hardware": "五金店",
        "electronics": "电子店",
        "clothing": "服装店",
        "shoes": "鞋店",
        "jewelry": "珠宝店",
        "pawnshop": "当铺",
        "antique": "古董店",
        "bookstore": "书店",
        "musicstore": "音乐店",
        "videostore": "音像店",
        "gymnasium": "体育馆",
        "pool": "游泳池",
        "sauna": "桑拿房",
        "spa": "水疗中心",
        "salon": "美容院",
        "barbershop": "理发店",
        "tattoo": "纹身店",
        "piercing": "穿孔店",
        "laundry": "洗衣店",
        "drycleaner": "干洗店",
        "repair": "修理店",
        "cardealer": "汽车经销商",
        "carrepair": "汽车修理店",
        "carwash": "洗车店",
        "rental": "租赁店",
        "travel": "旅行社",
        "insurance": "保险公司",
        "realestate": "房地产",
        "mortgage": "抵押贷款",
        "attorney": "律师事务所",
        "accountant": "会计师事务所",
        "consulting": "咨询公司",
        "advertising": "广告公司",
        "marketing": "营销公司",
        "printing": "印刷店",
        "shipping": "快递店",
        "postoffice": "邮局",
        "telecom": "电信公司",
        "utility": "公用事业",
        "recycling": "回收站",
        "scrapyard": "废料场",
        "landfill": "垃圾填埋场",
        "cemetery": "墓地",
        "funeral": "殡仪馆"
    }
    
    # 区域类型
    zones = {
        "TownZone": "城镇区域",
        "Forest": "森林",
        "DeepForest": "深林",
        "Vegitation": "植被区",
        "Farm": "农场",
        "FarmLand": "农田",
        "TrailerPark": "拖车公园",
        "Nav": "导航区",
        "Residential": "住宅区",
        "Commercial": "商业区",
        "Industrial": "工业区",
        "Rural": "乡村",
        "Wilderness": "荒野",
        "Mountain": "山区",
        "Beach": "海滩",
        "Lake": "湖泊",
        "River": "河流",
        "Swamp": "沼泽",
        "Desert": "沙漠",
        "Grassland": "草原",
        "Tundra": "苔原",
        "Glacier": "冰川",
        "Volcano": "火山",
        "Cave": "洞穴",
        "Underground": "地下",
        "Military": "军事区",
        "Restricted": "禁区",
        "Quarantine": "隔离区",
        "HazMat": "危险物质区",
        "Radiation": "辐射区",
        "Contaminated": "污染区",
        "Infected": "感染区",
        "SafeZone": "安全区",
        "Evacuation": "疏散区",
        "Emergency": "紧急区",
        "Disaster": "灾区"
    }
    
    # 对象类型
    object_types = {
        "IsoDoor": "门",
        "IsoWindow": "窗户",
        "IsoLightSwitch": "灯开关",
        "IsoRadio": "收音机",
        "IsoTelevision": "电视",
        "IsoGenerator": "发电机",
        "IsoStove": "炉灶",
        "IsoBarbecue": "烧烤架",
        "IsoFireplace": "壁炉",
        "IsoCompost": "堆肥箱",
        "IsoRainBarrel": "雨水桶",
        "IsoMannequin": "人体模型",
        "IsoThumpable": "可破坏物",
        "IsoContainer": "容器",
        "IsoBuilding": "建筑",
        "IsoRoom": "房间",
        "IsoFloor": "地板",
        "IsoWall": "墙壁",
        "IsoRoof": "屋顶",
        "IsoStairs": "楼梯",
        "IsoElevator": "电梯",
        "IsoEscalator": "自动扶梯",
        "IsoDoorFrame": "门框",
        "IsoWindowFrame": "窗框",
        "IsoFence": "栅栏",
        "IsoGate": "大门",
        "IsoSign": "标志",
        "IsoStreetLight": "路灯",
        "IsoTrafficLight": "交通灯",
        "IsoFireHydrant": "消防栓",
        "IsoMailbox": "邮箱",
        "IsoBench": "长椅",
        "IsoTable": "桌子",
        "IsoChair": "椅子",
        "IsoBed": "床",
        "IsoSofa": "沙发",
        "IsoCabinet": "柜子",
        "IsoDesk": "书桌",
        "IsoBookshelf": "书架",
        "IsoPlant": "植物",
        "IsoTree": "树",
        "IsoBush": "灌木",
        "IsoFlower": "花",
        "IsoGrass": "草",
        "IsoWater": "水",
        "IsoFire": "火",
        "IsoSmoke": "烟",
        "IsoExplosion": "爆炸",
        "IsoBlood": "血",
        "IsoCorpse": "尸体",
        "IsoItem": "物品",
        "IsoVehicle": "载具",
        "IsoAnimal": "动物",
        "IsoZombie": "僵尸",
        "IsoSurvivor": "幸存者",
        "IsoPlayer": "玩家"
    }
    
    output_data = {
        "_meta": {
            "category": "archive_content",
            "language": "zh_CN",
            "version": "1.0.0",
            "source": "manual",
            "description": "区块内容翻译 - 容器、建筑",
            "game_version": {
                "b41": "41.78.16",
                "b42": "42.0.0"
            },
            "last_updated": str(date.today()),
            "entry_count": len(containers) + len(buildings) + len(zones) + len(object_types)
        },
        "containers": containers,
        "buildings": buildings,
        "zones": zones,
        "object_types": object_types,
        "version_overrides": {},
        "mod_overrides": {}
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"已写入：{output_path}")


def main():
    """主函数。"""
    # 基础路径
    base_path = Path(__file__).parent.parent
    demo_path = base_path / "_demo"
    output_base = base_path / "resources" / "i18n"
    
    print("=" * 60)
    print("i18n 数据提取工具")
    print("=" * 60)
    
    # 1. 生成物品翻译
    print("\n[1/5] 生成物品翻译...")
    generate_items_json(
        demo_path / "b41" / "ProjectZomboid" / "media" / "lua" / "shared" / "Translate" / "CN" / "ItemName_CN.txt",
        demo_path / "b42" / "ProjectZomboid" / "media" / "lua" / "shared" / "Translate" / "CN" / "ItemName_CN.txt",
        output_base / "game" / "items_zh_CN.json"
    )
    
    # 2. 生成媒体翻译
    print("\n[2/5] 生成媒体翻译...")
    generate_media_json(
        demo_path / "b41" / "ProjectZomboid" / "media" / "lua" / "shared" / "Translate" / "CN" / "Recorded_Media_CN.txt",
        demo_path / "b42" / "ProjectZomboid" / "media" / "lua" / "shared" / "Translate" / "CN" / "Recorded_Media_CN.txt",
        output_base / "game" / "media_zh_CN.json"
    )
    
    # 3. 生成玩家数据翻译
    print("\n[3/5] 生成玩家数据翻译...")
    generate_player_json(output_base / "archive" / "player_zh_CN.json")
    
    # 4. 生成载具数据翻译
    print("\n[4/5] 生成载具数据翻译...")
    generate_vehicle_json(output_base / "archive" / "vehicle_zh_CN.json")
    
    # 5. 生成区块内容翻译
    print("\n[5/5] 生成区块内容翻译...")
    generate_content_json(output_base / "archive" / "content_zh_CN.json")
    
    print("\n" + "=" * 60)
    print("完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
