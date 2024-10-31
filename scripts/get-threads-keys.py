import environ
import requests

env = environ.FileAwareEnv()

APP_ID = env("THREADS_APP_ID")
APP_SECRET = env("THREADS_APP_SECRET")
THREADS_CALLBACK = env("THREADS_CALLBACK_URL")

AUTHORIZATION_BASE_URL = "https://threads.net/oauth/authorize"

SHORT_LIVED_ACCESS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
LONG_LIVED_ACCESS_TOKEN_URL = "https://graph.threads.net/access_token"

USER_INFO_BASE_URL = "https://graph.threads.net/v1.0"


def main():
    if not APP_ID or not APP_SECRET:
        raise Exception(
            "Please check your env file and make sure THREADS_APP_ID and THREADS_APP_SECRET are set"
        )

    authorization_url = (
        f"{AUTHORIZATION_BASE_URL}"
        f"?client_id={APP_ID}"
        f"&redirect_uri={THREADS_CALLBACK}"
        f"&scope=threads_basic,threads_content_publish"
        f"&response_type=code"
    )
    print(f"\nPlease go here and authorize: {authorization_url}")
    threads_code = input("\nPaste the PIN here: ")

    # Get a short-lived token first:
    response = requests.post(
        SHORT_LIVED_ACCESS_TOKEN_URL,
        data={"client_id": APP_ID,
              "client_secret": APP_SECRET,
              "code": threads_code,
              "grant_type": "authorization_code",
              "redirect_uri": THREADS_CALLBACK,},
    )

    if response.status_code != 200:
        raise Exception(
            f"Short-lived token request returned an error: {response.status_code} {response.text}"
        )

    # Exchange short-lived token for a long-lived one:
    short_lived_data = response.json()
    short_lived_access_token = short_lived_data.get("access_token")
    user_id = short_lived_data.get("user_id")

    params = {
        "grant_type": "th_exchange_token",
        "client_secret": APP_SECRET,
        "access_token": short_lived_access_token,
    }
    response = requests.get(LONG_LIVED_ACCESS_TOKEN_URL, params=params)

    if response.status_code != 200:
        raise Exception(
            f"Long-lived token request returned an error: {response.status_code} {response.text}"
        )

    long_lived_data = response.json()
    long_access_token = long_lived_data.get("access_token")
    expires_in = long_lived_data.get("expires_in")

    user_info_url = f"{USER_INFO_BASE_URL}/{user_id}?fields=username&access_token={long_access_token}"
    response = requests.get(user_info_url)

    if response.status_code != 200:
        raise Exception(
            f"User info request returned an error: {response.status_code} {response.text}"
        )

    user_info = response.json()
    username = user_info.get("username")

    print(
        "\nUse the admin panel to create a new channel with the following data:"
    )
    print("Service: Threads")
    print(f"Account: {username}")
    print(f"Account id: {user_id}")
    print("Enable: True")
    print(f"Access Token: {long_access_token}")
    if expires_in is not None:
        print(f"\nNote: Token will expire in {expires_in / 86400:.1f} days unless refreshed.")


if __name__ == "__main__":
    main()