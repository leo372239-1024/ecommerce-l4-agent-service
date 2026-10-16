"""Explicit seeded snapshot facts for the isolated merchant simulator."""


def details(order_id, paid):
    sku = "SKU-DEMO-01"
    return {
        "get_order_items": {
            "items": [
                {"sku": sku, "name": "演示保温杯", "quantity": 1, "allocated_paid_minor": paid}
            ]
        },
        "get_order_payment": {
            "method": "SIMULATED_CARD",
            "payment_id": "SIM-PAY-" + order_id,
            "status": "PAID",
        },
        "get_order_discounts": {
            "original_minor": paid + 1000,
            "discount_minor": 1000,
            "allocation_method": "single_item",
        },
        "get_order_invoice": {"status": "NOT_ISSUED", "type": "电子普通发票"},
        "get_order_address_masked": {
            "province": "浙江省",
            "city": "杭州市",
            "address": "演示街道***",
            "phone": "138****0000",
        },
        "get_order_timeline": {"events": [{"type": "PAID", "timestamp": "2026-10-01T02:00:00Z"}]},
        "get_shipment_tracking": {
            "tracking_id": "SIM-TRACK-" + order_id,
            "nodes": [{"status": "商家交运", "timestamp": "2026-10-02T02:00:00Z"}],
        },
        "get_shipment_packages": {
            "packages": [
                {"package_id": "SIM-PKG-" + order_id, "weight_grams": 400, "sku_ids": [sku]}
            ]
        },
        "get_carrier_contact": {"carrier": "演示物流", "hotline": None, "is_simulated": True},
        "get_delivery_estimate": {"estimate": "2026-10-10", "delayed": False},
        "get_return_instructions": {
            "warehouse": "演示退货仓",
            "instructions": "先创建退货申请，再寄回并等待验收",
            "shipping_address": "仅模拟，不可真实寄件",
        },
        "get_exchange_options": {"options": [{"sku": sku, "color": "银色", "available": 15}]},
        "get_reship_status": {"reshipments": []},
        "get_product_specs": {"sku": sku, "material": "不锈钢", "capacity_ml": 500},
        "get_product_warranty": {"days": 365, "coverage": "非人为产品质量问题"},
        "get_product_price": {"sku": sku, "current_price_minor": 13900, "currency": "CNY"},
        "get_product_inventory": {"sku": sku, "available_quantity": 15},
        "get_product_shipping": {"regions": ["中国大陆"], "fee_minor": 0, "currency": "CNY"},
        "get_coupon_rules": {
            "coupon_id": "DEMO-10",
            "min_spend_minor": 1000,
            "discount_minor": 1000,
        },
        "get_coupon_return_policy": {"restored_on_full_refund": True, "expires_unchanged": True},
        "get_ticket_status": {"tickets": []},
        "get_support_hours": {
            "timezone": "Asia/Shanghai",
            "hours": "09:00-18:00",
            "days": "周一至周五",
        },
        "get_return_shipping_fee": {
            "payer": "商家",
            "fee_minor": 800,
            "currency": "CNY",
            "is_estimate": True,
        },
        "get_refund_eta": {"min_business_days": 1, "max_business_days": 3, "method": "原路退回"},
    }
