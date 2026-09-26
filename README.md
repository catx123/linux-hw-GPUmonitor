# Linux HW GPU Monitor

A real-time, terminal-based hardware monitor for Linux. Features deep AMD GPU telemetry (Junction/Hotspot temps), NVIDIA/Intel support, and custom sensor mapping.

![Screenshot](screenshot.jpg)

## ✨ Features

* **Multi-Vendor Support:** Automatically detects and monitors AMD, NVIDIA (via `nvidia-smi`), and Intel GPUs.
* **Deep AMD Telemetry:** Dynamically reads and displays Edge, Junction (Hotspot), and Memory (VRAM) temperatures.
* **Visual Analytics:** ASCII sparklines show real-time trends for CPU/GPU utilization and Junction temperatures.
* **Smart Alerting:** Color-coded text (Cyan/Yellow/Red) warns you when Junction temperatures approach thermal throttling limits (90°C+).
* **Total Power Budget:** Calculates total system power draw by combining CPU RAPL, GPU power, and estimated fan power.
* **Custom Sensors:** Map any custom file path (like NVMe drives or water cooling pumps) via a simple JSON configuration.
* **Session Statistics:** Tracks Min, Max, and Average values for all metrics during your session.
* **CSV Logging:** Optional background logging to `hardware_log.csv` for historical analysis.

## 🚀 Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/catx123/linux-hw-GPUmonitor.git
   cd linux-hw-GPUmonitor
