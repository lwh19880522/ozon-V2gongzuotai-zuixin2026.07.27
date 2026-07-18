"use strict";

(function exposeSellerEvidence(root) {
  function normalized(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function signal(kind, rawText) {
    return { kind, raw_text: normalized(rawText) };
  }

  function hasDecisionEvidence(rawText) {
    const decision = classify(rawText);
    return decision.confidence === "high" && decision.is_chinese_domestic_seller !== null;
  }

  function classifyEvidence(evidence = {}) {
    const deliveryText = normalized(evidence.deliveryText);
    const globalText = normalized(evidence.globalText);
    const sellerText = normalized(evidence.sellerText);
    const attributeText = normalized(evidence.attributeText);
    const signals = [];

    const productOriginChina = attributeText.match(
      /(?:(?:страна\s+(?:производства|происхождения)|страна-изготовитель|country\s+of\s+origin)\s*["']?\s*[:：]?\s*["']?\s*(?:китай|china)|(?:原产国|生产国|制造国)\s*["']?\s*[:：]?\s*["']?\s*(?:中国|china))/iu,
    );
    if (productOriginChina) {
      const originSignal = signal("product_origin_china", productOriginChina[0]);
      return {
        is_chinese_domestic_seller: true,
        confidence: "high",
        signals: [originSignal],
        evidence: originSignal.raw_text,
      };
    }

    const localWarehouse = deliveryText.match(
      /(?:со\s+склада\s+ozon|склад\w*\s+ozon|достав\w*\s+(?:сегодня|завтра))/iu,
    );
    if (localWarehouse) {
      const localSignal = signal("local_warehouse_russia", localWarehouse[0]);
      return {
        is_chinese_domestic_seller: false,
        confidence: "high",
        signals: [localSignal],
        evidence: localSignal.raw_text,
      };
    }

    const localSellerWarehouse = deliveryText.match(
      /со\s+склада\s+продавца,?\s*(?:москв[а-яё]*|московск[а-яё\s]*област[а-яё]*|санкт-петербург[а-яё]*|ленинградск[а-яё\s]*област[а-яё]*|росси[а-яё]*)/iu,
    );
    if (localSellerWarehouse) {
      const localSignal = signal("seller_warehouse_russia", localSellerWarehouse[0]);
      return {
        is_chinese_domestic_seller: false,
        confidence: "high",
        signals: [localSignal],
        evidence: localSignal.raw_text,
      };
    }

    const chinaSellerWarehouse = deliveryText.match(
      /со\s+склада\s+продавца,?\s*(?:(?:anhui|fujian|gansu|guangdong|guizhou|hainan|hebei|heilongjiang|henan|hubei|hunan|jiangsu|jiangxi|jilin|liaoning|qinghai|shaanxi|shandong|shanxi|sichuan|yunnan|zhejiang)(?:\s+sheng)?|guangxi|inner\s+mongolia|ningxia|xinjiang|tibet|beijing|chongqing|shanghai|tianjin|shenzhen|guangzhou|dongguan|yiwu|wenzhou|shantou)/iu,
    );
    if (chinaSellerWarehouse) {
      signals.push(signal("seller_warehouse_china", chinaSellerWarehouse[0]));
    }

    const chinaDelivery = deliveryText.match(/(?:доставка|отправка|доставим)[^.!?]{0,80}из\s+китая/iu);
    if (chinaDelivery) {
      signals.push(signal("delivery_origin_china", chinaDelivery[0]));
    }

    const globalChina = globalText.match(/ozon\s*global[^.!?]{0,160}(?:китай|товар\w*\s+из\s+китая)/iu);
    if (globalChina) {
      signals.push(signal("ozon_global_china", globalChina[0]));
    }

    const chineseLegalEntity = sellerText.match(
      /(?:продавец|юридическ\w*\s+лиц\w*|компания)[^.!?]{0,180}(?:китай|шэньчжэнь|гуанчжоу|иу)/iu,
    );
    if (chineseLegalEntity) {
      signals.push(signal("seller_legal_entity_china", chineseLegalEntity[0]));
    }

    if (signals.length) {
      return {
        is_chinese_domestic_seller: true,
        confidence: "high",
        signals,
        evidence: signals.map((item) => item.raw_text).join(" | "),
      };
    }

    return {
      is_chinese_domestic_seller: null,
      confidence: "unknown",
      signals: [],
      evidence: "No strong Chinese cross-border seller evidence was visible.",
    };
  }

  function classify(rawText) {
    return classifyEvidence({
      deliveryText: rawText,
      globalText: rawText,
      sellerText: rawText,
      attributeText: rawText,
    });
  }

  const api = { classify, classifyEvidence, hasDecisionEvidence };
  root.OzonV2SellerEvidence = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
