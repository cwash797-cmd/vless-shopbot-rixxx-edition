import asyncio
import logging
import os
import signal
import threading
from pathlib import Path

from dotenv import load_dotenv
from waitress import create_server

load_dotenv(Path(__file__).resolve().parents[2] / '.env')

from shop_bot.webhook_server.app import create_webhook_app
from shop_bot.data_manager.scheduler import periodic_subscription_check
from shop_bot.data_manager import database
from shop_bot.bot_controller import BotController


def main():
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    database.initialize_db()
    controller = BotController()
    app = create_webhook_app(controller)

    async def run():
        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()
        controller.set_loop(loop)
        app.config['EVENT_LOOP'] = loop
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        server = create_server(app, host=os.environ.get('WEBHOOK_HOST', '127.0.0.1'),
                               port=int(os.environ.get('WEBHOOK_PORT', '1488')),
                               threads=4, max_request_body_size=256 * 1024)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        logging.info('Web admin listening; configure HTTPS reverse proxy before accepting payments')
        logging.info('Shop bot: %s', controller.start_shop_bot().get('message'))
        if database.get_setting('support_bot_token') and database.get_setting('support_group_id'):
            controller.start_support_bot()
        scheduler = asyncio.create_task(periodic_subscription_check(controller))
        try:
            await stopped.wait()
        finally:
            controller.stop_shop_bot()
            controller.stop_support_bot()
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            await asyncio.sleep(1)
            server.close()

    asyncio.run(run())


if __name__ == '__main__':
    main()
