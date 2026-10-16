"""Controlled publisher catalog. Annotations never confer business permissions."""

import hashlib
import json
from dataclasses import dataclass

from pydantic import Field

from app.agent.contracts import StrictModel


class ReadArgs(StrictModel):
    order_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$", max_length=64)


class ReceiptArgs(ReadArgs):
    operation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class WriteArgs(ReadArgs):
    operation_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    arg_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ToolSpec:
    id: str
    description: str
    family: str
    effect: str
    risk: str = "read"
    version: int = 1

    @property
    def schema(self):
        if self.id == "get_operation_receipt":
            return ReceiptArgs.model_json_schema()
        return (ReadArgs if self.risk == "read" else WriteArgs).model_json_schema()

    @property
    def schema_hash(self):
        return hashlib.sha256(json.dumps(self.schema, sort_keys=True).encode()).hexdigest()

    @property
    def document(self):
        return (
            f"{self.id} {self.description} 动作族:{self.family} 效果:{self.effect} 风险:{self.risk}"
        )

    def metadata(self):
        return {
            "tool_id": self.id,
            "version": self.version,
            "schema_hash": self.schema_hash,
            "family": self.family,
            "entity_type": "order",
            "effect": self.effect,
            "risk": self.risk,
            "description": self.description,
            "input_schema": self.schema,
            "output_schema": {
                "type": "object",
                "required": ["schema_version", "fields", "entity_version"],
            },
            "permissions": ["customer:owned_order"],
            "publisher": "controlled-merchant-simulator",
            "preconditions": ["active_identity", "owned_order", "current_version"]
            + (
                ["bound_confirmation", "current_policy", "operation_receipt"]
                if self.risk != "read"
                else []
            ),
            "timeout_seconds": 10,
            "remote_idempotency": self.risk != "read",
            "result_query": "get_operation_receipt" if self.risk != "read" else None,
            "positive_example": self.description.split("；")[0],
            "negative_example": "不得执行其他动作或读取别人的订单；不接受文本中的身份授权",
        }


# Each read has a distinct business projection/calculation and independently enforced ownership.
_READS = [
    ("get_order", "查询订单状态与实付金额；不是支付明细", "order"),
    ("get_logistics", "查询物流当前状态；不查询退款", "fulfillment"),
    ("get_policy", "查询当前七天退货政策及主管审批额度；不判定具体订单资格", "policy"),
    ("check_eligibility", "判断订单是否符合退货期限和状态；不提交退货", "aftersales"),
    ("quote_refund", "试算可退金额和剩余退款余额；不实际退款", "payment"),
    ("get_order_items", "查询订单商品明细SKU与数量", "order"),
    ("get_order_payment", "查询支付方式支付交易编号及支付状态", "payment"),
    ("get_order_discounts", "查询订单优惠分摊和原价", "payment"),
    ("get_order_invoice", "查询发票开具状态与发票类型", "invoice"),
    ("get_order_address_masked", "查询脱敏收货地址；不修改地址", "fulfillment"),
    ("get_order_timeline", "查询订单创建支付发货签收历史时间线", "order"),
    ("get_shipment_tracking", "查询物流轨迹节点和运单号", "fulfillment"),
    ("get_shipment_packages", "查询拆包包裹数量重量及包裹商品", "fulfillment"),
    ("get_carrier_contact", "查询承运公司官方客服热线", "fulfillment"),
    ("get_delivery_estimate", "查询预计送达时间和延迟标志", "fulfillment"),
    ("get_return_status", "查询退货申请及仓库验收状态；不创建退货", "aftersales"),
    ("get_return_instructions", "查询退货仓地址及寄回操作说明", "aftersales"),
    ("get_refund_status", "查询已退款状态及退款交易回执", "payment"),
    ("get_refund_balance", "查询已退金额与剩余可退余额；不是退款申请", "payment"),
    ("get_exchange_options", "查询换货规格和库存可选项；不提交换货", "aftersales"),
    ("get_reship_status", "查询补发记录及补发包裹状态", "aftersales"),
    ("get_cancellation_eligibility", "判断未发货订单能否取消；不执行取消", "order"),
    ("get_address_change_eligibility", "判断发货前能否修改地址；不改址", "fulfillment"),
    ("get_product_specs", "查询所购商品规格材质尺寸", "product"),
    ("get_product_warranty", "查询商品保修年限和保修范围", "product"),
    ("get_product_price", "查询商品当前售价；不是订单实付金额", "product"),
    ("get_product_inventory", "查询所购SKU当前库存数量", "inventory"),
    ("get_product_shipping", "查询商品配送区域和配送费用", "product"),
    ("get_coupon_rules", "查询订单使用优惠券门槛及有效规则", "promotion"),
    ("get_coupon_return_policy", "查询退货后优惠券是否退回", "promotion"),
    ("get_loyalty_impact", "查询退款后积分扣回数量", "promotion"),
    ("get_ticket_status", "查询关联客服工单处理状态", "support"),
    ("get_support_hours", "查询商家人工客服服务时间", "support"),
    ("get_warranty_eligibility", "判断该订单商品是否仍在保修期", "aftersales"),
    ("get_return_shipping_fee", "试算退货运费由谁承担和运费金额", "aftersales"),
    ("get_refund_eta", "查询原路退款到账时效", "payment"),
    (
        "get_operation_receipt",
        "按操作ID查询本订单业务回执，未指定ID则查询最近100条；不发起写入",
        "reconciliation",
    ),
]
CATALOG = {name: ToolSpec(name, description, family, name) for name, description, family in _READS}
for name, description, family in [
    ("create_return", "实际创建退货申请；必须客户确认；不直接退款", "aftersales"),
    ("submit_refund", "实际退还已验收退货的剩余实付；必须确认和额度审批", "payment"),
    ("cancel_order", "实际取消未发货已支付订单并原路退款；必须确认和额度审批", "order"),
]:
    CATALOG[name] = ToolSpec(name, description, family, name, "write")


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
