"use strict";

const assert = require("assert");
const path = require("path");

const evidence = require(path.join(
  __dirname,
  "..",
  "browser_extension",
  "ozon_v2_bridge",
  "seller_evidence.js",
));

const chinaDelivery = evidence.classify("Доставка из Китая, срок 15-30 дней");
assert.equal(chinaDelivery.is_chinese_domestic_seller, true);
assert.equal(chinaDelivery.confidence, "high");
assert.equal(chinaDelivery.signals[0].kind, "delivery_origin_china");

const ozonGlobal = evidence.classify("Ozon Global Международные товары Китай");
assert.equal(ozonGlobal.is_chinese_domestic_seller, true);
assert.equal(ozonGlobal.signals[0].kind, "ozon_global_china");

const chinaSellerWarehouse = evidence.classifyEvidence({
  deliveryText: "Доставка и возврат Москва Со склада продавца, Guangdong Sheng Курьерской службой партнёра С 22 июля Условия доставки из-за рубежа",
});
assert.equal(chinaSellerWarehouse.is_chinese_domestic_seller, true);
assert.equal(chinaSellerWarehouse.confidence, "high");
assert.equal(chinaSellerWarehouse.signals[0].kind, "seller_warehouse_china");

const russianSellerWarehouse = evidence.classifyEvidence({
  deliveryText: "Доставка и возврат Москва Со склада продавца, Московская область Курьером завтра",
});
assert.equal(russianSellerWarehouse.is_chinese_domestic_seller, false);
assert.equal(russianSellerWarehouse.confidence, "high");

const local = evidence.classify("Со склада Ozon, доставим завтра");
assert.equal(local.is_chinese_domestic_seller, false);
assert.equal(local.confidence, "high");

const russianProductOrigin = evidence.classifyEvidence({
  attributeText: "Страна производства: Китай",
});
assert.equal(russianProductOrigin.is_chinese_domestic_seller, true);
assert.equal(russianProductOrigin.confidence, "high");
assert.equal(russianProductOrigin.signals[0].kind, "product_origin_china");

const chineseProductOrigin = evidence.classifyEvidence({
  attributeText: "原产国：中国",
});
assert.equal(chineseProductOrigin.is_chinese_domestic_seller, true);
assert.equal(chineseProductOrigin.confidence, "high");
assert.equal(chineseProductOrigin.signals[0].kind, "product_origin_china");

const unrelatedChinaText = evidence.classify("Рекомендуем товары из Китая");
assert.equal(
  unrelatedChinaText.is_chinese_domestic_seller,
  null,
  "unscoped China text must not prove the current seller is cross-border",
);

const localWithIncidentalChina = evidence.classify(
  "Отзыв покупателя: посылка из Китая. Со склада Ozon, доставим завтра",
);
assert.equal(
  localWithIncidentalChina.is_chinese_domestic_seller,
  false,
  "local Ozon warehouse evidence must not be overridden by incidental China text",
);

const distantGlobalChina = evidence.classify(`Ozon Global ${"описание ".repeat(30)} Китай`);
assert.equal(
  distantGlobalChina.is_chinese_domestic_seller,
  null,
  "Ozon Global and China must appear in the same bounded evidence region",
);

assert.equal(
  evidence.hasDecisionEvidence("Название товара Цвет Материал Размер"),
  false,
  "title and attributes alone are not enough to classify seller origin",
);
assert.equal(
  evidence.hasDecisionEvidence("Доставка и возврат Москва Со склада Ozon"),
  true,
  "the delivery region makes the detail page ready for seller-origin classification",
);
assert.equal(
  evidence.hasDecisionEvidence("Магазин Продавец Ozon Global"),
  false,
  "generic seller labels without origin evidence must not make the detail page ready",
);
assert.equal(
  evidence.hasDecisionEvidence("Магазин Продавец Доставка из Китая"),
  true,
  "explicit China delivery evidence makes the detail page ready",
);
assert.equal(
  evidence.hasDecisionEvidence("Со склада продавца, Zhejiang Sheng Условия доставки из-за рубежа"),
  true,
  "a Chinese seller warehouse province is direct buyer-side origin evidence",
);

process.stdout.write("browser seller evidence: OK\n");
