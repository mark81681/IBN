# wifi 우선 기본값 예시
# sudo ip route replace default via 192.168.0.1 dev wlan0 metric 100
# sudo ip route replace default via 192.168.8.1 dev eth2 metric 600

import jetson_inference
import jetson_utils
import time
import threading
import asyncio
import json
import random
import subprocess

import websockets  # pip install --user websockets

WIDTH, HEIGHT = 640, 480

RTMP_URL = "rtmp://20.41.100.163/live/jetson1"

# WS는 VM1(게이트웨이)로 붙음 (VM1 -> VM2 프록시)
WS_URL = "ws://20.41.100.163/ws/jetson"
DEVICE_ID = "jetson1"

WIFI_IFACE = "wlan0"
LTE_IFACE = "eth2"
IFACES = [WIFI_IFACE, LTE_IFACE]

# 확인한 gateway 값
WIFI_GW = "192.168.0.1"
LTE_GW = "192.168.8.1"

current_policy = {
    "mode": "wifi_only",
    "wifi_ratio": 1.0,
    "lte_ratio": 0.0
}

# WS 스레드 -> 메인 루프에 적용 요청 전달용
pending_policy_apply = False
pending_policy_value = None

SAMPLE_INTERVAL_SEC = 1.0   # /proc/net/dev 샘플링 간격
SEND_INTERVAL_SEC = 2.0     # 서버로 METRICS 보내는 간격


def run_cmd(cmd):
    """
    리눅스 명령 실행 + 로그 출력
    """
    print("[CMD]", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print("[STDOUT]", result.stdout.strip())
    if result.stderr.strip():
        print("[STDERR]", result.stderr.strip())
    return result


def show_default_routes():
    """
    현재 default route 로그 출력
    """
    result = run_cmd(["ip", "route", "show", "default"])
    return result


def apply_route_policy(policy):
    """
    policy에 따라 default route metric 변경
    wifi_only / lte_only 우선 구현
    ratio는 아직 미구현
    """
    mode = policy.get("mode", "")

    if mode == "wifi_only":
        print("[ROUTE] applying wifi_only")
        run_cmd([
            "sudo", "ip", "route", "replace", "default",
            "via", WIFI_GW, "dev", WIFI_IFACE, "metric", "100"
        ])
        run_cmd([
            "sudo", "ip", "route", "replace", "default",
            "via", LTE_GW, "dev", LTE_IFACE, "metric", "600"
        ])
        show_default_routes()

    elif mode == "lte_only":
        print("[ROUTE] applying lte_only")
        run_cmd([
            "sudo", "ip", "route", "replace", "default",
            "via", LTE_GW, "dev", LTE_IFACE, "metric", "100"
        ])
        run_cmd([
            "sudo", "ip", "route", "replace", "default",
            "via", WIFI_GW, "dev", WIFI_IFACE, "metric", "600"
        ])
        show_default_routes()

    elif mode == "ratio":
        print("[ROUTE] ratio mode is not implemented yet. keeping current routes.")
        print("[ROUTE] requested policy:", policy)
        show_default_routes()

    else:
        print("[ROUTE] unknown mode:", mode)
        show_default_routes()


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
        rx_bytes = int(fields[0])   # RX bytes
        tx_bytes = int(fields[8])   # TX bytes
        out[iface] = (rx_bytes, tx_bytes)
    return out


def mbps(delta_bytes, dt_sec):
    if dt_sec <= 0:
        return 0.0
    return (delta_bytes * 8.0) / 1_000_000.0 / dt_sec


class WsMetricsClient:
    """
    WebSocket 연결 유지 + HELLO + 주기적 METRICS 전송.
    영상 루프를 방해하지 않도록 별도 스레드에서 asyncio로 실행.
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
        global current_policy, pending_policy_apply, pending_policy_value

        prev = read_proc_net_dev()
        prev_t = time.time()
        send_accum = 0.0

        async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
            # HELLO
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
                    msg = {
                        "type": "METRICS",
                        "device_id": self.device_id,
                        "ifaces": ifaces_payload,
                        "policy": current_policy,
                    }
                    await ws.send(json.dumps(msg))

                    try:
                        reply = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        print("[WS] reply:", reply)

                        data = json.loads(reply)
                        msg_type = data.get("type")

                        if msg_type == "POLICY":
                            policy = data.get("policy", {})
                            current_policy = policy

                            # 메인 루프에서 실제 적용하도록 예약
                            pending_policy_value = policy
                            pending_policy_apply = True

                            print("[WS] POLICY updated:", current_policy)

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
show_default_routes()

start = time.time()
cnt_local = 0
cnt_rtmp = 0

# WS metrics 클라이언트 시작
ws_client = WsMetricsClient(WS_URL, DEVICE_ID, IFACES)
ws_client.start()

try:
    while True:
        # 새 policy 적용 요청이 있으면 여기서 실제 route 변경 + RTMP 재연결
        if pending_policy_apply and pending_policy_value is not None:
            print("[MAIN] applying pending policy:", pending_policy_value)

            # 1) route 정책 적용
            apply_route_policy(pending_policy_value)

            # 2) 기존 RTMP 연결 닫기
            try:
                if display_rtmp:
                    display_rtmp.Close()
                    print("[MAIN] old RTMP output closed")
            except Exception as e:
                print("[MAIN] display_rtmp close error:", e)

            # 3) 잠깐 대기
            time.sleep(1.0)

            # 4) 새 RTMP 연결 생성
            try:
                display_rtmp = jetson_utils.videoOutput(RTMP_URL)
                print("[MAIN] new RTMP output created")
            except Exception as e:
                print("[MAIN] display_rtmp recreate error:", e)

            pending_policy_apply = False
            pending_policy_value = None

        img, w, h = camera.CaptureRGBA()

        if img is None:
            print("Capture Failed")
            break

        # 1. Detect
        dets = net.Detect(img, w, h)

        # 2. Render
        if display_local and display_local.IsStreaming():
            display_local.Render(img)
            display_local.SetStatus(f"{net.GetNetworkFPS():.0f} FPS")
            if cnt_local == 0:
                print("[local]=====================")
                cnt_local += 1

        if display_rtmp:
            display_rtmp.Render(img)
            if cnt_rtmp == 0:
                print("[rtmp]======================")
                cnt_rtmp += 1

        # 테스트용 종료(원하면 제거해서 무한 실행 가능)
        if time.time() - start > 120:
            print("Timeout Exit")
            break

except KeyboardInterrupt:
    print("Interrupted")

finally:
    # WS 종료
    try:
        ws_client.stop()
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
