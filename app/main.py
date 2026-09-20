import flet as ft

def main(page: ft.Page) -> None:
    page.title = "Yachiyo"
    page.window.width = 400
    page.window.height = 600
    page.add(ft.Text("kskbl\nzdjd"))

if __name__ == "__main__":
    ft.run(main)