from __future__ import annotations

import unittest

from ozon_v2.services.attribute_mapping_service import map_template_attributes


class AttributeMappingServiceTests(unittest.TestCase):
    def test_uses_complete_ozon_content_evidence_and_marks_creative_fields_for_rewrite(self) -> None:
        schema = [
            {"attribute_id": "85", "attribute_label": "Бренд", "is_required": True},
            {"attribute_id": "7194", "attribute_label": "Материал", "is_required": False},
            {"attribute_id": "9799", "attribute_label": "Ширина, мм", "is_required": False},
            {"attribute_id": "4180", "attribute_label": "Название", "is_required": False},
            {"attribute_id": "4191", "attribute_label": "Аннотация", "is_required": False},
            {"attribute_id": "11254", "attribute_label": "Rich-контент JSON", "is_required": False},
        ]
        candidate = {
            "title": "Исходный заголовок Ozon",
            "brand": "PRO SEWING",
            "attributes": {"Материал": "Сталь"},
            "content_score_evidence": {
                "attribute_table": {"Материал": "Сталь", "Ширина, мм": "45"},
                "description_or_rich_content_blocks": ["Исходное описание Ozon"],
            },
        }

        result = map_template_attributes(schema, candidate)
        fields = {field["field_key"]: field for field in result["fields"]}

        self.assertEqual("mapped", fields["85"]["status"])
        self.assertEqual("mapped", fields["7194"]["status"])
        self.assertEqual("mapped", fields["9799"]["status"])
        self.assertEqual("45", fields["9799"]["value"])
        self.assertEqual(
            "ozon.content_score_evidence.attribute_table.Ширина, мм",
            fields["9799"]["evidence_ref"],
        )
        self.assertEqual("rewrite_required", fields["4180"]["status"])
        self.assertEqual("rewrite_required", fields["4191"]["status"])
        self.assertEqual("rewrite_required", fields["11254"]["status"])
        self.assertEqual(3, result["mapped_attribute_count"])
        self.assertEqual(3, result["rewrite_required_count"])
        self.assertEqual(0, result["excluded_attribute_count"])

    def test_preserves_all_schema_rows_and_reports_every_status_bucket(self) -> None:
        schema = [
            {"attribute_id": "85", "attribute_label": "Бренд", "is_required": True},
            {"attribute_id": "4180", "attribute_label": "Название", "is_required": False},
            {"attribute_id": "4383", "attribute_label": "Вес товара, г", "is_required": False},
            {
                "attribute_id": "21845",
                "attribute_label": "Озон.Видеообложка: ссылка",
                "is_required": False,
            },
        ]

        result = map_template_attributes(schema, {"brand": "Test Brand", "attributes": {}})

        self.assertEqual(4, len(result["fields"]))
        self.assertEqual(1, result["mapped_attribute_count"])
        self.assertEqual(1, result["rewrite_required_count"])
        self.assertEqual(1, result["missing_fact_count"])
        self.assertEqual(1, result["not_applicable_count"])
        self.assertEqual(
            ["mapped", "rewrite_required", "missing_fact", "not_applicable"],
            [field["status"] for field in result["fields"]],
        )

    def test_uses_validated_original_content_for_creative_template_fields(self) -> None:
        schema = [
            {"attribute_id": "4180", "attribute_label": "Название", "is_required": False},
            {"attribute_id": "4191", "attribute_label": "Аннотация", "is_required": False},
        ]

        result = map_template_attributes(
            schema,
            {"title": "Исходный заголовок", "attributes": {"Материал": "Сталь"}},
            rewritten_content={
                "4180": "Пробойник для кожи из стали, комплект 3 в 1",
                "4191": "Инструмент для аккуратной работы с кожей и установки фурнитуры.",
            },
        )
        fields = {field["field_key"]: field for field in result["fields"]}

        self.assertEqual("mapped", fields["4180"]["status"])
        self.assertEqual("generated_original_content", fields["4180"]["source"])
        self.assertEqual("generated_content.fields.4180", fields["4180"]["evidence_ref"])
        self.assertEqual(2, result["mapped_attribute_count"])
        self.assertEqual(0, result["rewrite_required_count"])

    def test_supplier_truth_overrides_reference_identity_and_ignores_spec_table_artifacts(self) -> None:
        schema = [
            {"attribute_id": "brand", "attribute_label": "Бренд", "is_required": True},
            {"attribute_id": "model", "attribute_label": "Название модели", "is_required": True},
            {"attribute_id": "color", "attribute_label": "Цвет товара", "is_required": False},
            {"attribute_id": "material", "attribute_label": "Материал", "is_required": False},
            {"attribute_id": "length", "attribute_label": "Длина, мм", "is_required": False},
        ]
        candidate = {
            "brand": "PRO SEWING",
            "target_sku": {
                "selected_options": {
                    "Цвет": "Дырокол 3 в 1",
                    "Комплектация": "Дырокол-просекатель",
                }
            },
            "attributes": {
                "Материал": "Сталь",
                "Длина, мм": "210",
                "Артикул": "2097521796",
            },
        }
        supplier_product = {
            "attributes": {
                "品牌": "梅芳",
                "刃口材质": "碳钢",
                "型号": "全长(mm)",
                "1020（9寸多功能三合一皮带打孔钳）": "210",
                "全长": "全部 135 170 210 300",
            }
        }
        supplier_selection = {
            "supplier_sku": {
                "selected_options": {
                    "型号": "1020",
                    "规格": "9寸多功能三合一皮带打孔钳",
                    "全长": "210毫米",
                }
            }
        }

        result = map_template_attributes(
            schema,
            candidate,
            supplier_product=supplier_product,
            supplier_selection=supplier_selection,
        )
        fields = {field["field_key"]: field for field in result["fields"]}

        self.assertEqual("梅芳", fields["brand"]["value"])
        self.assertEqual("supplier_attributes", fields["brand"]["source"])
        self.assertEqual("1020", fields["model"]["value"])
        self.assertEqual("confirmed_supplier_sku", fields["model"]["source"])
        self.assertEqual("missing_fact", fields["color"]["status"])
        self.assertEqual("碳钢", fields["material"]["value"])
        self.assertEqual("210毫米", fields["length"]["value"])

    def test_uses_evidence_backed_intelligent_field_results_without_fabricating_unresolved_facts(
        self,
    ) -> None:
        schema = [
            {
                "attribute_id": "tools-count",
                "attribute_label": "Количество инструментов в наборе, шт.",
                "is_required": True,
            },
            {
                "attribute_id": "warranty",
                "attribute_label": "Гарантия",
                "is_required": False,
            },
            {
                "attribute_id": "title",
                "attribute_label": "Название",
                "is_required": False,
            },
        ]

        result = map_template_attributes(
            schema,
            {
                "title": "Исходный заголовок Ozon",
                "attributes": {"Комплектация": "1 инструмент"},
            },
            rewritten_content={
                "tools-count": {
                    "decision": "filled",
                    "value": "1",
                    "evidence_refs": ["ozon.attributes.Комплектация"],
                    "reason": "Количество извлечено из собранной комплектации.",
                },
                "warranty": {
                    "decision": "unresolved",
                    "reason": "В собранных данных Ozon и 1688 гарантия не указана.",
                    "evidence_refs": [],
                },
                "title": {
                    "decision": "filled",
                    "value": "Ручной инструмент для точной работы, комплект 1 шт.",
                    "evidence_refs": ["ozon.title", "ozon.attributes.Комплектация"],
                    "reason": "Оригинальный заголовок создан по собранным фактам.",
                },
            },
        )
        fields = {field["field_key"]: field for field in result["fields"]}

        self.assertEqual("mapped", fields["tools-count"]["status"])
        self.assertEqual("generated_evidence_completion", fields["tools-count"]["source"])
        self.assertEqual(["ozon.attributes.Комплектация"], fields["tools-count"]["evidence_refs"])
        self.assertEqual("1", fields["tools-count"]["value"])
        self.assertEqual("missing_fact", fields["warranty"]["status"])
        self.assertEqual("unresolved", fields["warranty"]["intelligence_decision"])
        self.assertIsNone(fields["warranty"]["value"])
        self.assertEqual("generated_original_content", fields["title"]["source"])

    def test_maps_locked_supplier_sku_quantity_and_stable_seller_code(self) -> None:
        schema = [
            {
                "attribute_id": "quantity",
                "attribute_label": "Количество товара в УЕИ",
                "is_required": True,
            },
            {
                "attribute_id": "seller-code",
                "attribute_label": "Код продавца",
                "is_required": True,
            },
        ]

        result = map_template_attributes(
            schema,
            {"seed_id": "seed-1599", "attributes": {}},
            supplier_product={"offer_id": "559479796544", "attributes": {}},
            supplier_selection={
                "supplier_offer_id": "559479796544",
                "supplier_sku": {
                    "supplier_sku_id": "559479796544",
                    "combination_key": "页面唯一 SKU",
                    "selected_options": {"规格": "页面唯一 SKU"},
                    "set_quantity": 1,
                    "set_composition": ["单件商品"],
                },
            },
        )
        fields = {field["field_key"]: field for field in result["fields"]}

        self.assertEqual("mapped", fields["quantity"]["status"])
        self.assertEqual(1, fields["quantity"]["value"])
        self.assertEqual("confirmed_supplier_sku", fields["quantity"]["source"])
        self.assertEqual(
            "supplier_selection.supplier_sku.set_quantity",
            fields["quantity"]["evidence_ref"],
        )
        self.assertEqual("mapped", fields["seller-code"]["status"])
        self.assertRegex(
            str(fields["seller-code"]["value"]),
            r"^OZV2-559479796544-[A-F0-9]{8}$",
        )
        self.assertEqual("workflow_generated", fields["seller-code"]["source"])

    def test_does_not_accept_chinese_generated_text_for_russian_model_field(
        self,
    ) -> None:
        result = map_template_attributes(
            [
                {
                    "attribute_id": "model",
                    "attribute_label": "Название модели (для объединения в одну карточку)",
                    "attribute_type": "String",
                    "is_required": False,
                }
            ],
            {"attributes": {}},
            supplier_product={"attributes": {"规格": "两用打孔钳"}},
            rewritten_content={
                "model": {
                    "decision": "filled",
                    "value": "两用打孔钳",
                    "evidence_refs": ["supplier.attributes.规格"],
                    "reason": "Copied from the supplier specification.",
                }
            },
        )
        field = result["fields"][0]

        self.assertEqual("missing_fact", field["status"])
        self.assertIsNone(field["value"])


if __name__ == "__main__":
    unittest.main()
