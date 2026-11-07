"""Slack proof: app_mention -> thread reply, button -> action handler, chat.update on parent."""
import os
import sys
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()
app = App(token=os.environ["SLACK_BOT_TOKEN"])
STEPS = ["🧾 ticket", "📜 spec", "🥄 tests", "🔥 implement", "✅ green"]


def progress(done: int) -> str:
    return " → ".join(("~" + s + "~") if i < done else (f"*{s}*" if i == done else s) for i, s in enumerate(STEPS))


@app.event("app_mention")
def on_mention(event, client, say):
    print(f"[proof] app_mention user={event.get('user')} channel={event['channel']} text={event.get('text')!r}", flush=True)
    thread_ts = event.get("thread_ts") or event["ts"]
    parent = client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, text="SpecGuard working…",
                                     blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": progress(0)}}])
    print("[proof] parent posted ts=" + parent["ts"], flush=True)
    client.chat_postMessage(
        channel=event["channel"], thread_ts=thread_ts, text="Approve spec?",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Spec ready.* Approve to continue?"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve spec"}, "style": "primary",
                 "action_id": "approve_spec", "value": parent["ts"]},
                {"type": "button", "text": {"type": "plain_text", "text": "Edit"}, "action_id": "edit_spec"},
            ]},
        ])


@app.action("approve_spec")
def on_approve(ack, body, client):
    ack()
    ch, parent_ts = body["channel"]["id"], body["actions"][0]["value"]
    print(f"[proof] button approve_spec user={body['user']['id']} channel={ch}", flush=True)
    for i in range(1, 5):
        client.chat_update(channel=ch, ts=parent_ts, text="SpecGuard working…",
                           blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": progress(i)}}])
        print(f"[proof] chat.update step={i}", flush=True)
        time.sleep(1)
    client.chat_postMessage(channel=ch, thread_ts=body["message"].get("thread_ts"), text="✅ Slack proof complete")


@app.action("edit_spec")
def on_edit(ack, body):
    ack()
    print("[proof] button edit_spec", flush=True)


@app.event("message")
def on_message(event):
    print(f"[proof] message event channel_type={event.get('channel_type')} thread={event.get('thread_ts')} text={event.get('text')!r}", flush=True)


if __name__ == "__main__":
    print("[proof] starting Socket Mode…", flush=True)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
