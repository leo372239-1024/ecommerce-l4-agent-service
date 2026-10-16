"""Publisher-maintained high precision business projections, shared across planning and routing.

Unknown paraphrases use hybrid retrieval. These rules never authorize writes or assert facts.
"""

import re

FACETS = {
    "get_shipment_tracking": r"物流轨迹(?:节点)?|运单号|物流节点|运输轨迹",
    "get_delivery_estimate": r"预计(?:送达|到达|到货)(?:时间)?|配送延迟|延迟标志",
    "get_order_invoice": r"发票(?:类型|开具状态|状态)",
    "get_refund_balance": r"(?:已退金额|剩余可退(?:金额|余额)|退款余额)",
    "get_loyalty_impact": r"积分(?:扣回|退回|影响|扣除)(?:数量)?|扣回积分",
    "get_order_discounts": r"(?:优惠|折扣)(?:金额|分摊|分配)",
    "get_order_payment": r"支付(?:方式|状态|交易)|付款方式|支付明细",
    "get_product_specs": r"(?:商品)?(?:规格|材质|容量|尺寸)",
    "get_product_inventory": r"(?:SKU)?(?:当前)?库存(?:数量)?",
    "get_product_price": r"(?:商品)?(?:当前)?(?:售价|价格)",
    "get_product_warranty": r"保修(?:年限|范围)|质保(?:年限|范围)",
    "get_warranty_eligibility": r"(?:是否|仍|还)(?:在)?保修期|保修资格",
    "get_coupon_return_policy": r"优惠券(?:是否)?(?:退回|返还)|退货后优惠券",
    "get_coupon_rules": r"优惠券(?:使用)?(?:门槛|有效期)|优惠券使用规则",
    "get_return_instructions": r"退货仓(?:地址)?|(?:退货)?寄回(?:操作)?说明",
    "get_return_shipping_fee": r"退货运费(?:承担|金额)?|寄回运费",
    "get_refund_eta": r"退款(?:到账|到帐)(?:时间|时效)?",
    "get_refund_status": r"退款(?:记录|状态|交易回执)",
    "get_return_status": r"退货(?:申请状态|验收状态)|仓库验收状态",
    "get_product_shipping": r"(?:商品)?(?:配送区域|配送费用)|商品运费",
    "get_address_change_eligibility": r"(?:能否|是否可以)(?:修改|更改|变更)(?:收货)?地址|改址资格",
    "get_cancellation_eligibility": r"(?:能否|是否可以)取消|取消资格",
    "get_order_address_masked": r"脱敏(?:收货)?地址|收货地址信息",
    "get_support_hours": r"(?:人工)?客服(?:服务)?(?:时间|工作时间)",
    "get_ticket_status": r"工单(?:处理)?状态",
    "get_reship_status": r"补发(?:记录|包裹|状态)",
    "get_exchange_options": r"换货(?:规格|库存|可选项|选项)",
    "get_shipment_packages": r"包裹(?:数量|重量|商品)|拆包信息",
    "get_carrier_contact": r"承运(?:公司)?(?:客服)?(?:热线|联系方式)|(?:承运公司|承运商|物流公司)(?:名称)?(?=$|[和与及、，。！？\s])",
    "get_order_timeline": r"订单(?:历史|时间线)|支付发货签收历史",
    "get_order_items": r"订单商品明细|商品清单|所购商品列表",
}


def facets(query):
    matches = {
        key: list(re.finditer(pattern, query))
        for key, pattern in FACETS.items()
        if re.search(pattern, query)
    }
    # Prefer a specific projection over a generic term contained inside it (e.g. 换货库存).
    return {
        key: [
            m
            for m in values
            if not any(
                other.start() <= m.start()
                and other.end() >= m.end()
                and other.end() - other.start() > m.end() - m.start()
                for other_key, others in matches.items()
                if other_key != key
                for other in others
            )
        ]
        for key, values in matches.items()
        if any(
            not any(
                other.start() <= m.start()
                and other.end() >= m.end()
                and other.end() - other.start() > m.end() - m.start()
                for other_key, others in matches.items()
                if other_key != key
                for other in others
            )
            for m in values
        )
    }


def split_query(query):
    matches = facets(query)
    if len(matches) < 2 or re.search(r"不要|无需|别查|不是|并非|不查", query):
        return [query]
    # Only conjunctions license procedural splitting; ambiguous comparisons remain model work.
    if not re.search(r"和|与|及|以及|还有|、|，|同时", query):
        return [query]
    spans = sorted((m.start(), m.end(), key) for key, values in matches.items() for m in values)
    fragments, seen = [], set()
    for start, end, key in spans:
        if key not in seen:
            fragments.append(query[start:end])
            seen.add(key)
    return fragments


def exact_effect(query):
    found = facets(query)
    # Comparisons/negation require semantic routing, rather than guessing from keyword presence.
    if len(found) == 1 and not re.search(r"不要|无需|别查|不是|并非|不查", query):
        return next(iter(found))
    return None
