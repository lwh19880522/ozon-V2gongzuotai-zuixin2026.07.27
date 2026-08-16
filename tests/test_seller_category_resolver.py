from __future__ import annotations

from ozon_v2.services.seller_category_resolver import SellerCategoryResolver


class FakeAdapter:
    def __init__(self, tree: list[dict] | dict[str, list[dict]]) -> None:
        self.trees = tree if isinstance(tree, dict) else {"DEFAULT": tree, "ZH_HANS": tree}
        self.requested_languages: list[str] = []

    def fetch_description_category_tree(self, language: str = "DEFAULT") -> list[dict]:
        self.requested_languages.append(language)
        return self.trees.get(language, self.trees.get("DEFAULT", []))


def truth() -> dict:
    return {
        "subject": {"value": "Automotive wet cleaning wipes"},
        "objective_fields": {
            "attributes": {"Use": "Car cleaning"},
            "selected_options": {"Pack": "80 wipes"},
        },
    }


def test_resolver_selects_one_official_type_from_supplier_truth() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            [
                {
                    "description_category_id": 10,
                    "type_id": 101,
                    "matched_category_path": "Automotive / Automotive wet wipes",
                },
                {
                    "description_category_id": 20,
                    "type_id": 202,
                    "matched_category_path": "Home / Paper towels",
                },
            ]
        )
    )

    result = resolver.resolve(truth(), seed_query_terms=["automotive wet wipes"])

    assert result["status"] == "resolved"
    assert result["chosen"]["description_category_id"] == 10
    assert result["source"] == "locked_1688_supplier_truth"


def test_resolver_returns_confirmation_for_ambiguous_official_types() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            [
                {"description_category_id": 10, "type_id": 101, "matched_category_path": "Home / Wipes"},
                {"description_category_id": 11, "type_id": 102, "matched_category_path": "Auto / Wipes"},
            ]
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "Cleaning wipes"},
            "objective_fields": {"selected_options": {"Pack": "80 wipes"}},
        },
        seed_query_terms=["wipes"],
    )

    assert result["status"] == "category_confirmation_required"
    assert result["chosen"] is None
    assert len(result["candidates"]) == 2


def test_resolver_uses_official_chinese_tree_and_ignores_supplier_company_title() -> None:
    adapter = FakeAdapter(
        {
            "ZH_HANS": [
                {
                    "description_category_id": 17028647,
                    "type_id": 91742,
                    "matched_category_path": "电子产品 / 摄影和摄像设备配件 / 镜头盖",
                },
                {
                    "description_category_id": 17028647,
                    "type_id": 91740,
                    "matched_category_path": "电子产品 / 摄影和摄像设备配件 / 相机配件",
                },
                {
                    "description_category_id": 17028968,
                    "type_id": 95238,
                    "matched_category_path": "宠物用品 / 宠物玩具",
                },
            ],
            "DEFAULT": [],
        }
    )
    resolver = SellerCategoryResolver(adapter)
    supplier_truth = {
        "subject": {"value": "深圳市苏杰明电子有限公司"},
        "objective_fields": {
            "attributes": {"适用产品": "DJI Action 6"},
            "selected_options": {"颜色": "ACTION 6镜头盖"},
        },
    }

    result = resolver.resolve(
        supplier_truth,
        seed_subject="相机电池仓取用拉片",
        seed_category_hint="相机配件",
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 91742
    assert result["chosen"]["matching_language"] == "ZH_HANS"
    assert adapter.requested_languages == ["ZH_HANS"]


def test_resolver_keeps_two_equally_supported_chinese_types_for_manual_confirmation() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "电子产品 / 相机配件 / 镜头盖",
                    },
                    {
                        "description_category_id": 11,
                        "type_id": 102,
                        "matched_category_path": "汽车用品 / 相机配件 / 镜头盖",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "ACTION 6镜头盖"},
            "objective_fields": {"selected_options": {"规格": "镜头盖"}},
        },
        seed_subject="相机镜头盖",
    )

    assert result["status"] == "category_confirmation_required"
    assert result["chosen"] is None
    assert [item["type_id"] for item in result["candidates"]] == [101, 102]


def test_seed_query_alone_cannot_override_unrelated_locked_supplier_truth() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "汽车用品 / 汽车湿巾",
                    }
                ],
                "DEFAULT": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "Automotive / Automotive wet wipes",
                    }
                ],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "原木色木珠手工材料"},
            "objective_fields": {"selected_options": {"直径": "6毫米"}},
        },
        seed_query_terms=["automotive wet wipes"],
    )

    assert result["status"] == "category_confirmation_required"
    assert result["chosen"] is None
    assert result["candidates"][0]["supplier_evidence_score"] < 0.55


def test_generic_commerce_attribute_does_not_count_as_category_evidence() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "电子产品 / 摄影配件 / 镜头盖",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "服装 / 内衣 / 产妇支持带",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "ACTION 6镜头盖"},
            "objective_fields": {
                "attributes": {
                    "是否支持一件代发": "支持",
                    "售后服务": "店面三包",
                    "适用机型": "ACTION 6",
                },
                "selected_options": {"颜色": "ACTION 6镜头盖"},
            },
        }
    )

    assert result["status"] == "resolved"
    support_belt = next(item for item in result["candidates"] if item["type_id"] == 202)
    assert support_belt["supplier_evidence_score"] < 0.55


def test_short_generic_leaf_cannot_tie_an_exact_product_type() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "食品 / 果汁、水、饮料 / 水",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "住宅和花园 / 炊具 / 水壶",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "自行车骑行水壶 公路车大容量运动水杯"},
            "objective_fields": {"selected_options": {"款式": "破风水壶"}},
        }
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 202


def test_parent_domain_and_specific_leaf_resolve_pet_grooming_product() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "美容和卫生 / 美发工具 / 梳子",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "小百货和配饰 / 发饰 / 梳子",
                    },
                    {
                        "description_category_id": 30,
                        "type_id": 303,
                        "matched_category_path": "宠物用品 / 宠物护理用品 / 宠物开结毛梳",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "猫咪除毛梳洗澡按摩猫梳子去浮毛兔子梳毛刷开结宠物用品"},
            "objective_fields": {"selected_options": {"适用对象": "猫狗宠物"}},
        }
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 303


def test_two_equally_specific_supplier_uses_still_require_confirmation() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "小百货和配饰 / 服装首饰 / 饰品配件",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "住宅和花园 / 窗帘和窗帘杆 / 窗帘",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "原木木圈木环 DIY饰品配件 窗帘圆环"},
            "objective_fields": {
                "attributes": {"用途": "饰品配件、窗帘圆环"},
                "selected_options": {"材质": "原木"},
            },
        }
    )

    assert result["status"] == "category_confirmation_required"
    assert result["chosen"] is None


def test_explicit_1688_product_category_overrides_broad_title_context() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "运动与休闲 / 自行车 / 自行车",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "住宅和花园 / 炊具 / 水壶",
                    },
                    {
                        "description_category_id": 30,
                        "type_id": 303,
                        "matched_category_path": "运动与休闲 / 旅游餐具 / 运动水壶",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "自行车骑行水壶 公路车大容量运动水杯"},
            "objective_fields": {
                "attributes": {"产品类别": "运动水壶"},
                "selected_options": {"颜色": "破风水壶+配套支架"},
            },
        }
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 303


def test_explicit_1688_category_distinguishes_product_from_nearby_accessory() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 30,
                        "type_id": 303,
                        "matched_category_path": "运动与休闲 / 旅游餐具 / 运动水壶",
                    },
                    {
                        "description_category_id": 31,
                        "type_id": 304,
                        "matched_category_path": "运动与休闲 / 旅游餐具 / 运动水壶盖",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "住宅和花园 / 炊具 / 水壶",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "自行车骑行水壶 公路车大容量运动水杯"},
            "objective_fields": {
                "attributes": {"产品类别": "运动水壶"},
                "selected_options": {"颜色": "破风水壶+配套支架"},
            },
        }
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 303


def test_specific_pet_grooming_leaf_wins_over_generic_pet_context() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "宠物用品 / 宠物护理用品 / 宠物开结毛梳",
                    },
                    {
                        "description_category_id": 20,
                        "type_id": 202,
                        "matched_category_path": "宠物用品 / 宠物餐具 / 宠物瓶",
                    },
                    {
                        "description_category_id": 21,
                        "type_id": 203,
                        "matched_category_path": "宠物用品 / 宠物餐具 / 宠物碗",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "猫咪除毛梳洗澡按摩猫梳子去浮毛兔子梳毛刷开结猫咪狗狗宠物用品"},
            "objective_fields": {"selected_options": {"适用对象": "猫狗宠物"}},
        }
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 101


def test_meaningful_chinese_candidates_do_not_mix_in_default_language_noise() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 10,
                        "type_id": 101,
                        "matched_category_path": "住宅和花园 / 镜子 / 室内装饰镜",
                    },
                    {
                        "description_category_id": 11,
                        "type_id": 102,
                        "matched_category_path": "住宅和花园 / 装饰和房间内饰 / 装饰画",
                    },
                ],
                "DEFAULT": [
                    {
                        "description_category_id": 99,
                        "type_id": 999,
                        "matched_category_path": "Electronics / 3D scanning / 3D scanner",
                    }
                ],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "镜子镜面墙贴 3D立体装饰圆镜"},
            "objective_fields": {"selected_options": {"规格": "圆形镜面墙贴"}},
        }
    )

    assert result["status"] == "category_confirmation_required"
    assert all(item["matching_language"] == "ZH_HANS" for item in result["candidates"])
    assert resolver.adapter.requested_languages == ["ZH_HANS"]


def test_reference_product_type_resolves_cross_language_category_and_keeps_chinese_label() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 17028927,
                        "type_id": 97619,
                        "matched_category_path": "乐器 / 乐器配件 / 音叉",
                    },
                    {
                        "description_category_id": 17028647,
                        "type_id": 91740,
                        "matched_category_path": "电子产品 / 摄影摄像配件 / 相机配件",
                    },
                    {
                        "description_category_id": 17028647,
                        "type_id": 91746,
                        "matched_category_path": "电子产品 / 摄影摄像配件 / 相机背带",
                    },
                    {
                        "description_category_id": 17028445,
                        "type_id": 971104255,
                        "matched_category_path": "电子产品 / VR设备及配件 / 虚拟现实配件",
                    },
                ],
                "DEFAULT": [
                    {
                        "description_category_id": 17028927,
                        "type_id": 97619,
                        "matched_category_path": (
                            "Музыкальные инструменты / Аксессуары к музыкальным "
                            "инструментам / Камертон"
                        ),
                    },
                    {
                        "description_category_id": 17028647,
                        "type_id": 91740,
                        "matched_category_path": (
                            "Электроника / Аксессуары для фото- и видеотехники / "
                            "Аксессуар для камеры"
                        ),
                    },
                    {
                        "description_category_id": 17028647,
                        "type_id": 91746,
                        "matched_category_path": (
                            "Электроника / Аксессуары для фото- и видеотехники / "
                            "Ремень для камеры"
                        ),
                    },
                    {
                        "description_category_id": 17028445,
                        "type_id": 971104255,
                        "matched_category_path": (
                            "Электроника / VR-устройства и аксессуары / VR-аксессуар"
                        ),
                    },
                ],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {"value": "合肥海獭数码科技有限公司"},
            "objective_fields": {
                "attributes": {
                    "适用机型": "Action 6",
                    "材质": "金属,光学玻璃",
                },
                "selected_options": {"规格": "单一 SKU"},
            },
        },
        seed_query_terms=["язычок для извлечения из батарейного отсека камеры"],
        seed_subject="相机电池仓取用拉片",
        seed_category_hint="摄影轻小配件",
        reference_product_type="Аксессуар для камеры",
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 91740
    assert result["chosen"]["display_category_path_zh"] == "电子产品 / 摄影摄像配件 / 相机配件"
    assert all(item["type_id"] != 97619 for item in result["candidates"])


def test_baking_mold_is_not_resolved_as_the_cake_food_it_produces() -> None:
    resolver = SellerCategoryResolver(
        FakeAdapter(
            {
                "ZH_HANS": [
                    {
                        "description_category_id": 17028773,
                        "type_id": 115949297,
                        "matched_category_path": "食品 / 面包和糖果点心 / 蛋糕",
                    },
                    {
                        "description_category_id": 17028732,
                        "type_id": 92470,
                        "matched_category_path": "住宅和花园 / 炊具 / 烘焙模具，烤盘",
                    },
                ],
                "DEFAULT": [],
            }
        )
    )

    result = resolver.resolve(
        {
            "subject": {
                "value": "6连麻绳花篮慕斯蛋糕模具 巧克力硅胶模具石膏摆件香薰蜡烛模具"
            },
            "objective_fields": {
                "attributes": {
                    "用途": "烘焙",
                    "材质": "硅胶",
                    "产品类别": "蛋糕模",
                },
                "selected_options": {"规格": "FCM（170g）樱桃 MCM-258"},
            },
        },
        seed_subject="杏仁核桃糖果模具",
        reference_product_type="Форма для льда, конфет",
    )

    assert result["status"] == "resolved"
    assert result["chosen"]["type_id"] == 92470
