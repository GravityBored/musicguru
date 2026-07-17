"""Publish the current track to external consumers (e.g. Home Assistant).

Two optional sinks, both no-ops unless configured:
  * webhook  -- AR_NOWPLAYING_WEBHOOK receives the track as a JSON POST
  * MQTT     -- AR_MQTT_HOST, only if paho-mqtt is installed (retained message)

Called from worker threads; never raises into the caller.
"""
import json
import logging

import requests

from . import config

log = logging.getLogger("audio_recognition.publish")

_mqtt = None
_mqtt_tried = False


def _get_mqtt():
    global _mqtt, _mqtt_tried
    if _mqtt is not None or _mqtt_tried:
        return _mqtt
    _mqtt_tried = True
    if not config.MQTT_HOST:
        return None
    try:
        import paho.mqtt.client as mqtt  # optional dependency
        c = mqtt.Client()
        if config.MQTT_USER:
            c.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD or "")
        c.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        c.loop_start()
        _mqtt = c
        log.info("MQTT connected to %s:%s", config.MQTT_HOST, config.MQTT_PORT)
    except Exception as e:  # ImportError, connection refused, auth, ...
        log.warning("MQTT unavailable (%s); skipping", e)
        _mqtt = None
    return _mqtt


def publish(payload: dict) -> None:
    if not (config.NOWPLAYING_WEBHOOK or config.MQTT_HOST):
        return
    body = json.dumps(payload)
    if config.NOWPLAYING_WEBHOOK:
        try:
            requests.post(
                config.NOWPLAYING_WEBHOOK,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=config.PUBLISH_TIMEOUT,
            )
        except requests.RequestException as e:
            log.debug("webhook publish failed: %s", e)
    client = _get_mqtt()
    if client is not None:
        try:
            client.publish(config.MQTT_TOPIC, body, retain=True)
        except Exception as e:
            log.debug("mqtt publish failed: %s", e)


def now_playing(track: dict) -> None:
    publish({
        "state": "playing",
        **{k: track.get(k) for k in
           ("title", "artist", "album", "genre", "duration", "cover_url")},
    })


def stopped() -> None:
    publish({"state": "idle", "title": None, "artist": None})
