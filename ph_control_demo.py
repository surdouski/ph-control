from automation import Automation2040W

import asyncio
import time
import sys
from machine import Pin, PWM, reset

from mqtt_as import MQTTClient, config
from primitives import set_global_exception

from usniffs import Sniffs
from sr_band import SRBand
from mpstore import load_store, write_store


board = Automation2040W()
store = load_store()
print(f"store: {store}")

config["client_id"] = store.get("client_id")
config["ssid"] = store.get("ssid")
config["wifi_pw"] = store.get("wifi_pw")
config["server"] = store.get("server")
config["port"] = store.get("port")
config["user"] = store.get("user")
config["password"] = store.get("password")
config["queue_len"] = store.get("queue_len")
config["ssl"] = store.get("ssl")
client = MQTTClient(config)

location = str(store.get("location") or "test")
ph_band = SRBand(
    target=float(store.get("ph_target") or 5.75),
    high=float(store.get("ph_high") or 6.25),
    low=float(store.get("ph_low") or 5.25),
)
dose_interval_ms = int(
    store.get("dose_interval_ms") or (1_000 * 60 * 30)
)  # Time between doses (30min default)
dose_period_ms = int(
    store.get("dose_period_ms") or 312
)  # Time pump stays on per dose (312ms, tests show approx 0.5ml dispensed at 50% duty)

PH_UP = NO = 1
PH_DOWN = NC = 0


class PeristalticDriver:
    SOURCING_OUTPUT = 0  # labeled on board as SOURCING OUTPUT 1
    RELAY = 0

    def __init__(self, NO_or_NC: int):
        self.NO_or_NC = NO_or_NC
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        board.output(self.SOURCING_OUTPUT, 0)  # 0 duty
        board.change_output_freq(self.SOURCING_OUTPUT, 1_000)

    async def dose(self, ms: int) -> None:
        async with self.lock:
            self._on()
            await asyncio.sleep_ms(ms)
            self._off()
            self.ready.set()

    def _on(self) -> None:
        board.relay(self.RELAY, self.NO_or_NC)  # Change (or stay) to the correct relay
        board.output(self.SOURCING_OUTPUT, 50)  # 32_767 duty

    def _off(self) -> None:
        board.output(self.SOURCING_OUTPUT, 0)  # 0 duty


ph_up_driver = PeristalticDriver(PH_UP)
ph_down_driver = PeristalticDriver(PH_DOWN)

sniffs = Sniffs()


@sniffs.route(location + "/reservoir_ph")
async def incoming_ph_reading(message) -> float:
    """Receives a ph reading and returns the float, or returns -1 if the message was not a valid float."""
    print(f"received ph reading: {message}")
    if isinstance(message, float):
        return message
    if isinstance(message, str) | isinstance(message, bytes):
        try:
            return float(message)
        except ValueError:
            print(
                f"{location}/reservoir_ph received a message that was not a float: {message}"
            )
    return -1


@sniffs.route(
    location + "/reservoir_ph/<config_value>:{dose_interval_ms,dose_period_ms}"
)
async def update_dose_settings(message, config_value):
    """Config settings for dose interval and dose period. Units are in milliseconds."""
    print(f"received dose setting update for {config_value}: {message}")
    data = message
    # Validation
    if isinstance(data, str) or isinstance(data, bytes) or isinstance(data, float):
        try:
            data = int(message)
        except ValueError:
            print(
                f"{location}/reservoir_ph/{config_value} received a message that was not an int: {message}"
            )
    if not isinstance(data, int):
        return

    # Update
    if config_value == "dose_interval_ms":
        global dose_interval_ms
        dose_interval_ms = data
        write_store("dose_interval_ms", data)
    elif config_value == "dose_period_ms":
        global dose_period_ms
        dose_period_ms = data
        write_store("dose_period_ms", data)


@sniffs.route(location + "/reservoir_ph/<ph_setting>:{ph_low,ph_high,ph_target}")
async def update_ph_settings(message, ph_setting):
    """Config settings for pH low/high and target. Units are floats and represent pH."""
    print(f"received ph setting update for {ph_setting}: {message}")
    data = message
    # Validation
    if isinstance(data, str) or isinstance(data, bytes) or isinstance(data, int):
        try:
            data = float(message)
        except ValueError:
            print(
                f"{location}/reservoir_ph/{ph_setting} received a message that was not an float: {message}"
            )
    if not isinstance(data, float):
        return

    # Update
    try:
        if ph_setting == "ph_low":
            ph_band.update_low(data)
            write_store("ph_low", data)
        elif ph_setting == "ph_high":
            ph_band.update_high(data)
            write_store("ph_high", data)
        elif ph_setting == "ph_target":
            ph_band.update_target(data)
            write_store("ph_target", data)
    except Exception as e:
        print(f"Caught exception while in update_ph_settings")


async def publish_alert(alert_level: str, message: str) -> None:
    """
    Publishes a message to the MQTT broker, for alerting any entities subscribed to
    the notification channels.
    (Copied from main board repo.)
    """
    await client.publish(f"{location}/{alert_level.lower()}/alert", message)
    # await client.publish(f"south/{alert_level.lower()}/alert", message)  # Uncomment when testing


async def dose_ph_up() -> None:
    while True:
        await ph_band.rise_event.wait()  # Wait until the event for doing a dose of pH up
        ph_band.rise_event.clear()
        print("dose up")
        await ph_up_driver.dose(dose_period_ms)
        asyncio.create_task(
            publish_alert("info", f"Low pH ({ph_band.value}) detected, adding pH up.")
        )  # Don't need to block, allow next cycle countdown to start before publishing


async def dose_ph_down() -> None:
    while True:
        await ph_band.fall_event.wait()  # Wait until the event for doing a dose of pH down
        ph_band.fall_event.clear()
        print("dose down")
        await ph_down_driver.dose(dose_period_ms)
        asyncio.create_task(
            publish_alert(
                "info", f"High pH ({ph_band.value}) detected, adding pH down."
            )
        )  # Don't need to block, allow next cycle countdown to start before publishing


async def ph_management_procedure() -> None:
    while True:
        await asyncio.sleep_ms(dose_interval_ms)
        data = await incoming_ph_reading
        print(f"Current pH: {data}")
        if data > 0:
            ph_band.run(data)  # Will trigger any needed events


async def main():
    set_global_exception()
    try:
        print("Establishing connection to network and MQTT broker...")
        await sniffs.bind(client)
        await client.connect()

        asyncio.create_task(ph_management_procedure())
        asyncio.create_task(dose_ph_up())
        asyncio.create_task(dose_ph_down())

        while True:
            await asyncio.sleep(100)
    except Exception as e:
        sys.print_exception(e)
        time.sleep(1)
        client.disconnect()
        reset()


try:
    asyncio.run(main())
finally:
    time.sleep(1)
    asyncio.new_event_loop()
