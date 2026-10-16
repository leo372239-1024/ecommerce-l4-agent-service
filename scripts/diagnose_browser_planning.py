"""Replay one existing task's planner input without business effects; retain real usage."""

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = """
import asyncio,json,sys
from datetime import datetime,timezone
from sqlalchemy import text
import httpx
import app.agent.planner as planner
from app.context.conversation import ConversationService,compact_turns
from app.core.config import get_settings
from app.core.resources import Resources
async def main():
 settings=get_settings(); resource=Resources(settings)
 try:
  async with resource.engine.connect() as connection:
   row=(await connection.execute(text("SELECT message,customer_id,conversation_id,conversation_seq FROM agent_runs WHERE id=:id"),{'id':sys.argv[1]})).mappings().one()
  turns=await ConversationService(resource.engine).turns(row['customer_id'],row['conversation_id'],before_seq=row['conversation_seq'])
  packet=compact_turns(turns)
  attempts=[]
  original=httpx.AsyncClient
  class ObservedClient(original):
   async def post(self,url,**kwargs):
    response=await super().post(url,**kwargs)
    attempts.append({'request':kwargs['json'],'http_status':response.status_code,'response':response.json()})
    return response
  httpx.AsyncClient=ObservedClient
  original_ground=planner.grounded_spec
  def observe_ground(*args,**kwargs):
   try:
    result=original_ground(*args,**kwargs)
    attempts[-1]['validation']='valid'
    return result
   except Exception as error:
    attempts[-1]['validation']=planner.validation_reason(error)
    raise
  planner.grounded_spec=observe_ground
  report={'captured_at':datetime.now(timezone.utc).isoformat(),'original_workflow_id':sys.argv[1],'scope':'One diagnostic planner replay using original historical prefix; no merchant calls, no score mutation, actual billed model calls retained.'}
  try:
   spec,usage=await planner.Planner(settings).parse(row['message'],conversation=packet)
   report.update(spec=spec.model_dump(mode='json'),usage=usage)
  except planner.PlanningError as error:
   report.update(error=str(error),usage=error.usage)
  report['attempts']=attempts
  print(json.dumps(report,ensure_ascii=True))
 finally:
  await resource.close()
asyncio.run(main())
"""


def main(args):
    output = args.output
    assert not output.exists(), "do not overwrite diagnosis"
    command = ["docker", "exec", "-i", "l4-agent-api-1", "python", "-", args.workflow]
    environment = os.environ.copy()
    if args.image:
        environment["AGENT_BACKEND_IMAGE"] = args.image
        command = [
            "docker",
            "compose",
            "--project-name",
            "l4-agent",
            "--env-file",
            str(ROOT / ".env"),
            "--env-file",
            str(ROOT / "infra/images.env"),
            "-f",
            str(ROOT / "infra/docker-compose.yml"),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "api",
            "python",
            "-",
            args.workflow,
        ]
    response = subprocess.run(
        command,
        input=SOURCE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=environment,
    )
    report = json.loads(response.stdout)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "error": report.get("error"),
                "attempts": [a.get("validation") for a in report["attempts"]],
                "usage": report.get("usage"),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--image",
        help="Isolated one-off API image for known-case development; normal services unchanged",
    )
    main(parser.parse_args())
