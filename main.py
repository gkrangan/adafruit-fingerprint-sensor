from app import FingerprintApp


def main() -> None:
    app = FingerprintApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
