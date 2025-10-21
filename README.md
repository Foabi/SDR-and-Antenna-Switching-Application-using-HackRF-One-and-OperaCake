# SDR and Antenna Switching Application using HackRF One & OperaCake

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)
![GNU Radio](https://img.shields.io/badge/GNU%20Radio-3.10-orange?logo=gnuradio)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Ubuntu%2022.04-green)
![License](https://img.shields.io/badge/License-Open--Source-brightgreen)
![Status](https://img.shields.io/badge/Status-Active-success)

---

## Overview

This project implements an **advanced, modular Software-Defined Radio (SDR)** platform for **real-time spectrum analysis** and **dynamic antenna management**.  
It utilizes the **HackRF One SDR** and the **OperaCake RF switch** to create a cost-effective, multi-mode RF monitoring tool.

Developed as a **diploma project** by *Fabian-Adrian Unguruşan* at the *Technical University of Cluj-Napoca*, the system features:

- A custom **PyQt5 GUI**
- **GNU Radio** backend for DSP
- **Real-time spectrum control and visualization**

---

## Key Capabilities

### 🔹 Wideband Spectrum Stitching
Extends the HackRF One’s **20 MHz instantaneous bandwidth** by dynamically sweeping and stitching multiple FFT windows — up to **4 GHz** total span.

### 🔹 Dynamic Antenna Switching
Seamless control of the **8-port OperaCake switch** with three flexible modes:

- 🖱️ **Manual Switching** — Directly select any antenna port  
- 📡 **Frequency Switching** — Automatically switch ports based on center frequency  
- ⏱️ **Time Switching** — Cycle through ports with configurable dwell times  

### 🔹 Real-Time Anomaly Detection
Detects spectrum anomalies such as **jamming** or **heavy utilization** using:

- **AdB Occupancy**
- **Spectral Flatness Measure (SFM)**  

Automatically switches to another antenna port when anomalies are detected.

### 🔹 GUI-Driven Control
A **responsive PyQt5 interface** provides:

- Live spectrum and waterfall plots  
- Real-time SDR parameter configuration  
  *(frequency, sample rate, gains, FFT size, etc.)*

---

## Setup & Installation

### 1. Environment Setup
**Recommended OS:** Ubuntu 22.04 LTS  
💡 *Tip:* Use a **Virtual Machine (VM)** with **USB passthrough** for stable hardware access.

---

### 2. Dependencies

#### System Packages
```bash
sudo apt update
sudo apt install -y cmake g++ git python3-dev python3-pip \
libboost-all-dev libgmp-dev swig qtchooser qtbase5-dev \
qtbase5-dev-tools libfftw3-dev python3-mako python3-numpy \
python3-scipy python3-apt python3-click python3-click-plugins python3-zmq
```
#### Python Libraries
```bash
python3 -m pip install --upgrade pip
python3 -m pip install PyQt5 pyqtgraph matplotlib QDarkStyle
```

### 3. GNU Radio & HackRF Tools
- A. Install GNU Radio
```bash
sudo apt install gnuradio
```
- B. Compile & Install gr-osmosdr
```bash
git clone https://github.com/osmocom/gr-osmosdr.git
cd gr-osmosdr
mkdir build && cd build
cmake ..
make -j$(nproc)
sudo make install
sudo ldconfig
```
- C. Compile & Install HackRF Tools
```bash
git clone https://github.com/greatscottgadgets/hackrf.git
cd hackrf/host
mkdir build && cd build
cmake ..
make -j$(nproc)
sudo make install
sudo ldconfig
```

### 4. Clone This Repository
```bash
git clone https://github.com/Foabi/SDR-and-Antenna-Switching-Application-using-HackRF-One-and-OperaCake.git
cd SDR-and-Antenna-Switching-Application-using-HackRF-One-and-OperaCake
```

## Usage
### Launch the Application
```bash
python3 GUI.py
```
The main controller GUI (HackRF OperaCake Controller) will appear - choose your desired operational mode.
## Operational Modes
| Mode | Description |
|------|--------------|
| **Manual Switching** | Manually select antenna ports (e.g., `A4`, `B1`) |
| **Frequency Switching** | Automatically switch based on center frequency (e.g., `101–200 MHz`) |
| **Time Switching** | Cycle through ports with custom dwell times |
| **Wide Spectrum Mode** | Sweep between custom start/end frequencies for stitched scanning |
| **Event Detection & Switching** | Detect anomalies (AdB, Occ, SFM) and auto-switch ports |

---

## 🧑‍💻 Author

**Fabian-Adrian Unguruşan**  
🎓 *Technical University of Cluj-Napoca*  
**Faculty:** Electronics, Telecommunications and Information Technology  
**Specialization:** Technologies and Telecommunication Systems (English Stream)  
**Graduation Year:** 2025  
🔗 [LinkedIn Profile](https://www.linkedin.com) *(replace with your actual link)*  

---

## 📄 License

This project is **open source**.  
Please refer to the repository for full licensing details.
