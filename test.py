import jetson_inference
import jetson_utils
import time

WIDTH, HEIGHT = 640, 480

net = jetson_inference.detectNet("ssd-mobilenet-v2", threshold=0.5)

camera = jetson_utils.gstCamera(WIDTH, HEIGHT, "/dev/video0")

display_local = jetson_utils.videoOutput()
display_rtmp = jetson_utils.videoOutput("rtmp://104.208.89.250/live/jetson1")


print("[Start] ====================== Ready to start object detection and streaming..")

start = time.time()
cnt_local = 0
cnt_rtmp = 0

try:
    while True:
        img, w, h = camera.CaptureRGBA()

        if img is None:
            print("Capture Failed"); break

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
            if cnt_rtmp ==- 0:
                print("[rtmp]======================")
                cnt_rtmp += 1

        if time.time() - start > 120:
            print("Timeout Exit"); break

except KeyboardInterrupt:
    print("Interrupted")

finally:
    try: camera.close()
    except: pass

    try:
        if display_local: display_local.Close()
    except: pass

    try:
        if display_rtmp: display_rtmp.Close()
    except: pass

    print("Done.")
