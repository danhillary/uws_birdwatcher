"""List available audio input devices so you can pick one for BW_INPUT_DEVICE."""
import sounddevice as sd


def main():
    default_in = sd.default.device[0]
    print("Available input devices (index: name):\n")
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            marker = "  <- default" if idx == default_in else ""
            print(f"  [{idx}] {dev['name']}  "
                  f"({dev['max_input_channels']} ch, "
                  f"{int(dev['default_samplerate'])} Hz){marker}")
    print("\nSet BW_INPUT_DEVICE=<index> to choose one (default is used otherwise).")


if __name__ == "__main__":
    main()
