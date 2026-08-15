import requests


def get_qrcode_image(url):
    api_url = "https://api.qrserver.com/v1/create-qr-code/"

    params = {
        "data": url,
        "size": "500x500",
        "margin": 20,
        "format": "png"
    }

    try:
        response = requests.get(
            api_url,
            params=params,
            timeout=10
        )

        if response.status_code == 200:
            return response.url

        return "https://via.placeholder.com/500?text=QRCode+Error"

    except Exception:
        return "https://via.placeholder.com/500?text=Network+Error"
