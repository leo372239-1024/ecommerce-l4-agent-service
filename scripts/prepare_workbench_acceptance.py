"""Create isolated simulated orders for manual browser acceptance; preserve prior fixtures."""

import json
import uuid

from verify_agent_core import ROOT, compose


def main():
    suffix = uuid.uuid4().hex[:8]
    orders = {name: f"ORD-S8{name.upper()}-{suffix}" for name in ("return", "refund", "reject")}
    code = f"""
import asyncio,json
from sqlalchemy import text
from app.core.config import get_settings
from app.core.resources import Resources
from app.business.fixtures import details
async def main():
 r=Resources(get_settings())
 try:
  async with r.engine.begin() as c:
   for name,oid in {orders!r}.items():
    p={{'status':'DELIVERED','paid_minor':12900 if name=='return' else 249900,'currency':'CNY','delivered_days':2,'logistics':'已签收','return_state':'NONE' if name=='return' else 'ACCEPTED'}}
    p['details']=details(oid,p['paid_minor'])
    await c.execute(text("INSERT INTO commerce_orders VALUES(:id,'customer-alice',1,CAST(:p AS jsonb))"),{{'id':oid,'p':json.dumps(p,ensure_ascii=False)}})
 finally: await r.close()
asyncio.run(main())
"""
    compose("exec", "-T", "api", "python", "-", input=code)
    out = ROOT / "artifacts/step8/browser-fixtures.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(orders, indent=2), encoding="utf-8")
    print(json.dumps(orders))


if __name__ == "__main__":
    main()
