#!/usr/bin/env python3

import requests


def check_service(name, url, timeout=2):
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code == 200:
            return "Running"
        else:
            return f"Stopped (HTTP {response.status_code})"
    except requests.exceptions.RequestException:
        return "Stopped"


def main():
    print("Service Status:")
    print()

    services = [
        ("LLM Server (:8080)", "http://localhost:8080/health"),
        ("Chat (Gradio) (:7860)", "http://localhost:7860"),
        ("OpenWebUI (:3000)", "http://localhost:3000"),
    ]

    for name, url in services:
        status = check_service(name, url)
        status_symbol = "[OK]" if status == "Running" else "[DOWN]"
        print(f"{status_symbol} {name}: {status}")

    print()
    print("Commands:")
    print("  mise start      - Start all services")
    print("  mise down      - Stop all services")
    print("  mise sl        - Start LLM server only")
    print("  mise sc        - Start Chat (Gradio) only")
    print("  mise xc        - Stop Chat (Gradio) only")
    print("  mise sw        - Start OpenWebUI only")
    print("  mise xw        - Stop OpenWebUI only")


if __name__ == "__main__":
    main()
