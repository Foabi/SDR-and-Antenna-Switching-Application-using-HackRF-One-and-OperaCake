import subprocess
import time
from datetime import datetime

def switch_port(port):
    start_time = datetime.now()
    print(f"[{start_time}] >>> Switching to {port}")
    proc = subprocess.run(["hackrf_operacake.exe", "-o", "0", "-a", port], capture_output=True)
    end_time = datetime.now()
    print(f"[{end_time}] <<< Switch to {port} complete")
    latency_ms = (end_time - start_time).total_seconds() * 1000
    return latency_ms

NUM_CYCLES = 50
latencies = []

for i in range(NUM_CYCLES):
    latency_a4 = switch_port("A4")
    time.sleep(.5)  # adjust as needed
    latency_b4 = switch_port("B4")
    time.sleep(.5)  # adjust as needed
    latencies.append(latency_a4)
    latencies.append(latency_b4)

print("\n--- Latency statistics over all switches ---")
print(f"Max latency: {max(latencies):.2f} ms")
print(f"Min latency: {min(latencies):.2f} ms")
print(f"Average latency: {sum(latencies)/len(latencies):.2f} ms")
