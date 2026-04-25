import jetson_inference
import jetson_utils
import time
import threading
import asyncio
import json
import random
import socket

import websockets  # pip install --user websockets

WIDTH, HEIGHT = 640, 480

# RTMP는 그대로 유지
RTMP_URL = "rtmp://20.41.100.163/live/jetson1"

# WebSocket도 그대로 유지
WS_URL = "ws://20.41.100.163/ws/jetson"
DEVICE_ID = "jetson1"

WIFI_IFACE = "wlan0"
LTE_IFACE = "eth2"
IFACES = [WIFI_IFACE, LTE_IFACE]

# 각 인터페이스의 gateway
WIFI_GW = "192.168.0.1"
LTE_GW = "192.168.8.1"

# 그래프용 UDP 테스트 트래픽 설정
# 각 gateway로 보내면 해당 인터페이스를 통해 나감
TRAFFIC_TARGET_PORT = 9999
TOTAL_TEST_TRAFFIC_BPS = 2_000_000   # 2 Mbps
UDP_PAYLOAD_SIZE = 1200              # bytes
TRAFFIC_TICK_SEC = 0.05              # 50ms

SAMPLE_INTERVAL_SEC = 1.0   # /proc/net/dev 샘플링 간격
SEND_INTERVAL_SEC = 2.0     # 서버로 METRICS 보내는 간격

policy_lock = threading.Lock()
current_policy = {
    "mode": "wifi_only",
    "wifi_ratio": 1.0,
    "lte_ratio": 0.0
}


def get_policy():
    with policy_lock:
        return dict(current_policy)


def set_policy(policy):
    global current_policy
    with policy_lock:
        current_policy = dict(policy)


def read_proc_net_dev():
    """
    /proc/net/dev에서 인터페이스별 누적 RX/TX 바이트를 읽는다.
    반환: {iface: (rx_bytes, tx_bytes)}
    """
    out = {}
    with open("/proc/net/dev", "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line in lines[2:]:
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        iface = iface.strip()
        fields = data.split()
        rx_bytes = int(fields[0])
        tx_bytes = int(fields[8])
        out[iface] = (rx_bytes, tx_bytes)
    return out


def mbps(delta_bytes, dt_sec):
    if dt_sec <= 0:
        return 0.0
    return (delta_bytes * 8.0) / 1_000_000.0 / dt_sec


class UdpTrafficGenerator:
    """
    RTMP와 별개로, 그래프를 움직이기 위한 테스트 UDP 트래픽 생성기.
    policy에 따라 WIFI_GW / LTE_GW 로 UDP를 보냄.
    """
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.payload = b"x" * UDP_PAYLOAD_SIZE

        self.wifi_target = (WIFI_GW, TRAFFIC_TARGET_PORT)
        self.lte_target = (LTE_GW, TRAFFIC_TARGET_PORT)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self):
        self.thread.start()
        print("[TRAFFIC] generator started")

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        try:
            self.sock.close()
        except Exception:
            pass
        print("[TRAFFIC] generator stopped")

    def _thread_main(self):
        try:
            self._run()
        except Exception as e:
            print("[TRAFFIC] error:", repr(e))

    def _run(self):
        bytes_per_tick = int((TOTAL_TEST_TRAFFIC_BPS / 8.0) * TRAFFIC_TICK_SEC)
        packets_per_tick = max(1, bytes_per_tick // UDP_PAYLOAD_SIZE)

        while not self.stop_event.is_set():
            policy = get_policy()
            mode = policy.get("mode", "wifi_only")

            if mode == "wifi_only":
                self._send_packets(self.wifi_target, packets_per_tick)

            elif mode == "lte_only":
                self._send_packets(self.lte_target, packets_per_tick)

            elif mode == "ratio":
                wifi_ratio = float(policy.get("wifi_ratio", 0.5) or 0.5)
                lte_ratio = float(policy.get("lte_ratio", 0.5) or 0.5)

                total = wifi_ratio + lte_ratio
                if total <= 0:
                    wifi_ratio, lte_ratio = 0.5, 0.5
                    total = 1.0

                wifi_ratio /= total
                lte_ratio /= total

                wifi_packets = int(round(packets_per_tick * wifi_ratio))
                lte_packets = max(0, packets_per_tick - wifi_packets)

                if wifi_packets > 0:
                    self._send_packets(self.wifi_target, wifi_packets)
                if lte_packets > 0:
                    self._send_packets(self.lte_target, lte_packets)

            time.sleep(TRAFFIC_TICK_SEC)

    def _send_packets(self, target, n):
        for _ in range(n):
            try:
                self.sock.sendto(self.payload, target)
            except Exception as e:
                print("[TRAFFIC] send error:", repr(e))
                break


class WsMetricsClient:
    """
    WebSocket 연결 유지 + HELLO + 주기적 METRICS 전송.
    policy 수신 시 current_policy 갱신.
    """
    def __init__(self, ws_url: str, device_id: str, ifaces):
        self.ws_url = ws_url
        self.device_id = device_id
        self.ifaces = list(ifaces)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _thread_main(self):
        asyncio.run(self._run())

    async def _run(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except Exception as e:
                print("[WS] error:", repr(e))
                wait = backoff + random.random() * 0.2
                print(f"[WS] reconnecting in {wait:.1f}s...")
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 30.0)

    async def _run_once(self):
        prev = read_proc_net_dev()
        prev_t = time.time()
        send_accum = 0.0

        async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"type": "HELLO", "device_id": self.device_id}))
            hello_ack = await ws.recv()
            print("[WS] HELLO_ACK:", hello_ack)

            while not self.stop_event.is_set():
                await asyncio.sleep(SAMPLE_INTERVAL_SEC)

                now = read_proc_net_dev()
                now_t = time.time()
                dt = now_t - prev_t

                ifaces_payload = {}
                for iface in self.ifaces:
                    if iface not in now or iface not in prev:
                        continue
                    rx0, tx0 = prev[iface]
                    rx1, tx1 = now[iface]
                    ifaces_payload[iface] = {
                        "rx_mbps": round(mbps(rx1 - rx0, dt), 6),
                        "tx_mbps": round(mbps(tx1 - tx0, dt), 6),
                    }

                prev = now
                prev_t = now_t
                send_accum += SAMPLE_INTERVAL_SEC

                if send_accum >= SEND_INTERVAL_SEC:
                    send_accum = 0.0

                    print("[METRICS SEND]", ifaces_payload, "| policy=", get_policy())

                    msg = {
                        "type": "METRICS",
                        "device_id": self.device_id,
                        "ifaces": ifaces_payload,
                        "policy": get_policy(),
                    }
                    await ws.send(json.dumps(msg))

                    try:
                        reply = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        print("[WS] reply:", reply)

                        data = json.loads(reply)
                        msg_type = data.get("type")

                        if msg_type == "POLICY":
                            policy = data.get("policy", {})
                            set_policy(policy)
                            print("[WS] POLICY updated:", get_policy())

                        elif msg_type == "NOOP":
                            pass

                        elif msg_type == "ERROR":
                            print("[WS] server error:", data)

                    except asyncio.TimeoutError:
                        pass


# -------------------- 기존 영상 코드 --------------------
net = jetson_inference.detectNet("ssd-mobilenet-v2", threshold=0.5)
camera = jetson_utils.gstCamera(WIDTH, HEIGHT, "/dev/video0")

display_local = jetson_utils.videoOutput()
display_rtmp = jetson_utils.videoOutput(RTMP_URL)

print("[Start] ====================== Ready to start object detection and streaming..")
print("[INFO] RTMP is untouched.")
print("[INFO] Policy affects only extra UDP test traffic for graph verification.")

start = time.time()
cnt_local = 0
cnt_rtmp = 0

# 그래프용 테스트 트래픽 생성기 시작
traffic_gen = UdpTrafficGenerator()
traffic_gen.start()

# WS metrics 클라이언트 시작
ws_client = WsMetricsClient(WS_URL, DEVICE_ID, IFACES)
ws_client.start()

try:
    while True:
        img, w, h = camera.CaptureRGBA()

        if img is None:
            print("Capture Failed")
            break

        dets = net.Detect(img, w, h)

        if display_local and display_local.IsStreaming():
            display_local.Render(img)

            policy = get_policy()
            mode = policy.get("mode", "unknown")
            display_local.SetStatus(f"{net.GetNetworkFPS():.0f} FPS | {mode}")

            if cnt_local == 0:
                print("[local]=====================")
                cnt_local += 1

        if display_rtmp:
            display_rtmp.Render(img)
            if cnt_rtmp == 0:
                print("[rtmp]======================")
                cnt_rtmp += 1

        # 테스트용 종료 (원하면 제거해서 무한 실행 가능)
        if time.time() - start > 120:
            print("Timeout Exit")
            break

except KeyboardInterrupt:
    print("Interrupted")

finally:
    try:
        ws_client.stop()
    except Exception:
        pass

    try:
        traffic_gen.stop()
    except Exception:
        pass

    try:
        camera.close()
    except Exception:
        pass

    try:
        if display_local:
            display_local.Close()
    except Exception:
        pass

    try:
        if display_rtmp:
            display_rtmp.Close()
    except Exception:
        pass

    print("Done.")
