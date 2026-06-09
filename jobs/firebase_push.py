import os
import json
import firebase_admin
from firebase_admin import credentials, messaging

def init_firebase():
    if firebase_admin._apps:
        return

    service_account_info = json.loads(
        os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
    )

    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)

def send_push_notification(fcm_token: str, title: str, body: str):
    init_firebase()

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        token=fcm_token,
    )

    response = messaging.send(message)
    return response