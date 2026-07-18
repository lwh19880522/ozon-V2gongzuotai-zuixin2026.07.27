"use strict";

const assert = require("assert");
const path = require("path");

const evidence = require(path.join(
  __dirname,
  "..",
  "browser_extension",
  "ozon_v2_bridge",
  "product_evidence.js",
));

assert.equal(evidence.crossBorderSearchQuery("цветочный горшок"), "цветочный горшок из Китая");
assert.equal(evidence.crossBorderSearchQuery("цветочный горшок из Китая"), "цветочный горшок из Китая");

assert.deepEqual(
  evidence.parseSellerEvidence({
    rawText: "Магазин Shenzhen Home Подписаться Заказы",
    url: "https://www.ozon.ru/seller/shenzhen-home-1/",
    linkText: "",
  }),
  {
    name: "Shenzhen Home",
    url: "https://www.ozon.ru/seller/shenzhen-home-1/",
    raw_text: "Магазин Shenzhen Home Подписаться Заказы",
  },
);

assert.deepEqual(
  evidence.parseDeliveryEvidence("Доставка и возврат Товар из Китая 15-30 дней Ozon Global"),
  {
    raw_text: "Доставка и возврат Товар из Китая 15-30 дней Ozon Global",
    origin: "Товар из Китая",
    time: "15-30 дней",
    fulfillment: "Ozon Global",
  },
);

assert.deepEqual(
  evidence.parseDeliveryEvidence(
    "Доставка и возврат Москва Со склада продавца, Guangdong Sheng Курьерской службой партнёра С 22 июля Условия доставки из-за рубежа",
  ),
  {
    raw_text: "Доставка и возврат Москва Со склада продавца, Guangdong Sheng Курьерской службой партнёра С 22 июля Условия доставки из-за рубежа",
    origin: "Со склада продавца, Guangdong Sheng",
    time: "С 22 июля",
    fulfillment: "cross_border_foreign_seller_warehouse",
  },
);

assert.deepEqual(
  evidence.prioritizeCrossBorderSearchLinks([
    { href: "https://www.ozon.ru/product/local-1/", text: "Local", card_text: "Доставка завтра" },
    { href: "https://www.ozon.ru/product/china-2/", text: "China", card_text: "Товар из Китая 15-30 дней" },
    { href: "https://www.ozon.ru/product/global-3/", text: "Global", card_text: "Ozon Global" },
  ]),
  [
    { href: "https://www.ozon.ru/product/china-2/", text: "China", card_text: "Товар из Китая 15-30 дней" },
    { href: "https://www.ozon.ru/product/global-3/", text: "Global", card_text: "Ozon Global" },
  ],
);

assert.deepEqual(
  evidence.prioritizeCrossBorderSearchLinks([
    { href: "https://www.ozon.ru/product/foreign-4/", text: "Foreign", card_text: "Доставка из-за рубежа, конец месяца" },
    { href: "https://www.ozon.ru/product/foreign-5/", text: "Foreign", card_text: "Зарубежный товар, следующий месяц" },
  ]).map((item) => item.href),
  [
    "https://www.ozon.ru/product/foreign-4/",
    "https://www.ozon.ru/product/foreign-5/",
  ],
  "Ozon overseas-delivery card labels must be eligible for strict detail verification",
);

assert.deepEqual(
  evidence.selectSearchCandidates([
    { href: "https://www.ozon.ru/product/local-1/", card_text: "Доставка завтра" },
    { href: "https://www.ozon.ru/product/local-2/", card_text: "Доставка послезавтра" },
  ], 1),
  [{ href: "https://www.ozon.ru/product/local-1/", card_text: "Доставка завтра" }],
  "when cards expose no overseas label, a bounded detail fallback must inspect candidates without accepting them",
);

const mixedCandidates = [
  { href: "https://www.ozon.ru/product/china-1/", card_text: "Ozon Global" },
  ...Array.from({ length: 9 }, (_, index) => ({
    href: `https://www.ozon.ru/product/visible-${index + 2}/`,
    card_text: "Visible Ozon result",
  })),
];
assert.deepEqual(
  evidence.selectSearchCandidates(mixedCandidates, 8).map((item) => item.href),
  mixedCandidates.slice(0, 8).map((item) => item.href),
  "a qualified first card must not prevent checking the other visible candidates up to the fixed limit",
);

assert.deepEqual(
  evidence.reconcileTemplateProgress(
    {
      dispatchToken: "old-token",
      seedIndex: 2,
      stage: "detail",
      templates: [{ seed_id: "seed-a" }, { seed_id: "seed-b" }],
      searchEvidence: { productLinks: [{ href: "https://www.ozon.ru/product/rejected/" }] },
      productLinkIndex: 7,
      rejectedCandidates: [{ href: "https://www.ozon.ru/product/rejected/" }],
    },
    [{ seed_id: "seed-a" }, { seed_id: "seed-b" }, { seed_id: "seed-replacement" }],
    "new-token",
  ),
  {
    dispatchToken: "new-token",
    seedIndex: 2,
    stage: "search",
    templates: [{ seed_id: "seed-a" }, { seed_id: "seed-b" }],
    searchEvidence: null,
    productLinkIndex: 0,
    rejectedCandidates: [],
  },
  "replacing one failed seed must preserve accepted templates and resume at the replacement slot",
);

const product = evidence.parseProductJsonLd([
  JSON.stringify({
    "@type": "Product",
    name: "Большой коврик для мыши",
    sku: "1627168495",
    image: [
      "https://cdn.ozon.ru/product/main.jpg",
      "https://cdn.ozon.ru/product/detail.jpg",
    ],
    brand: { name: "MousePro" },
    offers: { price: "799", priceCurrency: "RUB", availability: "https://schema.org/InStock" },
    aggregateRating: { ratingValue: "4.9", reviewCount: "2819" },
  }),
]);

assert.equal(product.title, "Большой коврик для мыши");
assert.equal(product.sku_id, "1627168495");
assert.equal(product.price, "799");
assert.equal(product.currency, "RUB");
assert.equal(product.rating, "4.9");
assert.equal(product.review_count, 2819);
assert.deepEqual(product.images, [
  "https://cdn.ozon.ru/product/main.jpg",
  "https://cdn.ozon.ru/product/detail.jpg",
]);

const attributes = evidence.normalizeAttributePairs([
  ["Материал", "Резина"],
  ["Размер", "800 x 300 x 3 мм"],
  ["Материал", "Резина"],
  ["", "ignored"],
]);
assert.deepEqual(attributes, {
  "Материал": "Резина",
  "Размер": "800 x 300 x 3 мм",
});

assert.deepEqual(
  evidence.normalizeMedia([
    "https://cdn.ozon.ru/product/main.jpg",
    "https://cdn.ozon.ru/icons/favicon.png",
    "data:image/png;base64,abc",
    "https://cdn.ozon.ru/product/main.jpg",
  ]),
  ["https://cdn.ozon.ru/product/main.jpg"],
);

const complete = {
  product_id: "1627168495",
  title: product.title,
  sku_id: product.sku_id,
  selected_options: { "Цвет": "Белый" },
  category_path: "Электроника / Аксессуары / Коврики для мыши",
  leaf_category: "Коврики для мыши",
  category_url: "https://www.ozon.ru/category/kovriki-dlya-myshi-15743/",
  category_id: "15743",
  attributes,
  main_gallery_images: product.images,
  selected_sku_images: [product.images[0]],
  price: product.price,
  currency: product.currency,
  rating: product.rating,
  review_count: product.review_count,
  seller_name: "CN Seller",
  seller_url: "https://www.ozon.ru/seller/cn-seller-1/",
  delivery_origin: "Доставка из Китая",
  delivery_time: "15-30 дней",
  fulfillment_label: "Ozon Global",
};
assert.deepEqual(evidence.missingPublicFields(complete), []);

assert.equal(
  typeof evidence.blockingCandidateFields,
  "function",
  "candidate selection must distinguish missing delivery display fields from missing China-origin proof",
);
assert.deepEqual(
  evidence.blockingCandidateFields(
    ["delivery_origin", "delivery_time", "fulfillment_label"],
    {
      is_chinese_domestic_seller: true,
      confidence: "high",
      signals: [{ kind: "product_origin_china", raw_text: "Страна-изготовитель: Китай" }],
    },
  ),
  [],
  "product origin China is sufficient for candidate selection when only delivery display fields are unavailable",
);
assert.deepEqual(
  evidence.blockingCandidateFields(
    ["attributes", "delivery_origin", "delivery_time", "fulfillment_label"],
    {
      is_chinese_domestic_seller: true,
      confidence: "high",
      signals: [{ kind: "product_origin_china", raw_text: "Страна-изготовитель: Китай" }],
    },
  ),
  ["attributes"],
  "China origin must not waive core product evidence",
);
assert.deepEqual(
  evidence.blockingCandidateFields(
    ["query_intent_mismatch"],
    {
      is_chinese_domestic_seller: true,
      confidence: "high",
      signals: [{ kind: "product_origin_china", raw_text: "Страна-изготовитель: Китай" }],
    },
  ),
  ["query_intent_mismatch"],
  "China origin must not waive query relevance",
);

assert.equal(
  evidence.hasUsedProductId([{ ozon_product_id: "3590708630" }], "3590708630"),
  true,
  "the browser must not assign one Ozon product to multiple seeds in a batch",
);
assert.equal(evidence.hasUsedProductId([{ ozon_product_id: "3590708630" }], "3067025732"), false);

assert.equal(
  evidence.isExcludedProductId(["3590708630", "3067025732"], "3590708630"),
  true,
  "a blacklisted or retained Ozon product must not be selected for a replacement seed",
);
assert.equal(evidence.isExcludedProductId(["3590708630"], "3067025732"), false);

assert.equal(
  evidence.matchesQueryIntent(["подвесная рейка для хранения"], {
    title: "Этикетка",
    category_path: "Товары для офиса / Этикетки",
    leaf_category: "Этикетки",
    attributes: { "Комплектация": "10 х подвесных стержней" },
  }),
  false,
  "a complete cross-border product must still be rejected when it does not match the seed query",
);
assert.equal(
  evidence.matchesQueryIntent(["сумка-органайзер для хранения обуви"], {
    title: "Мешок для хранения обуви",
    category_path: "Хранение / Органайзеры",
    leaf_category: "Мешки для хранения",
  }),
  true,
);
assert.equal(
  evidence.matchesQueryIntent(["подвесная рейка для хранения"], {
    title: "Мешок для хранения",
    category_path: "Дом и сад / Хранение вещей / Мешочки и пакеты",
    leaf_category: "Мешочки и пакеты",
  }),
  false,
  "generic storage wording must not make an unrelated product relevant",
);
assert.equal(
  evidence.matchesQueryIntent(["мусорные пакеты"], {
    title: "Мешки для мусора 46 л, 20мкм, 100 шт",
    category_path: "Дом и сад / Хозяйственные товары / Мешки для мусора",
    leaf_category: "Мешки для мусора",
  }),
  true,
  "bag synonyms may differ when the distinguishing product intent still matches",
);

assert.ok(
  evidence.missingPublicFields({ ...complete, attributes: { "Материал": "visible_on_detail_page" } })
    .includes("attributes"),
  "placeholder attributes must fail completeness",
);

const mergedViewportEvidence = evidence.mergePublicSnapshots(
  {
    title: "Органайзер",
    seller_name: "CN Seller",
    seller_evidence: "Магазин CN Seller",
    delivery_origin: "Со склада продавца, Guangdong Sheng",
    delivery_time: "С 22 июля",
    fulfillment_label: "cross_border_foreign_seller_warehouse",
    delivery_evidence: "Условия доставки из-за рубежа",
    attributes: {},
    main_gallery_images: ["https://cdn.ozon.ru/product/top.jpg"],
  },
  {
    title: "Органайзер",
    seller_name: null,
    seller_evidence: "",
    delivery_origin: null,
    delivery_time: null,
    fulfillment_label: null,
    delivery_evidence: "",
    attributes: { "Материал": "Пластик" },
    main_gallery_images: ["https://cdn.ozon.ru/product/detail.jpg"],
  },
);
assert.equal(mergedViewportEvidence.delivery_origin, "Со склада продавца, Guangdong Sheng");
assert.equal(mergedViewportEvidence.delivery_time, "С 22 июля");
assert.equal(mergedViewportEvidence.seller_name, "CN Seller");
assert.deepEqual(mergedViewportEvidence.attributes, { "Материал": "Пластик" });
assert.deepEqual(mergedViewportEvidence.main_gallery_images, [
  "https://cdn.ozon.ru/product/top.jpg",
  "https://cdn.ozon.ru/product/detail.jpg",
]);

process.stdout.write("browser product evidence: OK\n");
