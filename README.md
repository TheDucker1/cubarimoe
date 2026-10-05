# Cubari.moe
An image proxy powered by the Cubari reader.

This branch includes encrypted server chapter downloads. Follow [the setup guide](docs/chapter-download-setup.md) before starting Django: private configuration is required, and missing values intentionally block startup. The [design](docs/encrypted-chapter-downloads.md) and [implementation progress](docs/implementation-progress.md) describe behavior and verification.

Testing Supported By<br/>
<img width="160" src="http://foundation.zurb.com/sites/docs/assets/img/logos/browser-stack.svg" alt="BrowserStack"/>

## Prerequisites 
- git
- Python 3.8 (the verified runtime for this branch)
- pip
- virtualenv

## Install
1. Create a venv for cubarimoe in your home directory.
```
virtualenv ~/cubarimoe
```

2. Clone cubarimoe's source code into the venv.
```
git clone https://github.com/appu1232/cubarimoe ~/cubarimoe/app
```

3. Activate the venv.
```
cd ~/cubarimoe/app && source ../bin/activate
```

4. Install cubarimoe's dependencies.
```
pip3 install -r requirements.txt
```

5. Configure the required secrets and storage using [the private configuration guide](docs/chapter-download-setup.md). Keep `SECRET_KEY` in private configuration along with the chapter password and storage credentials.

6. Generate the default assets for cubarimoe.
```
python3 init.py
```

7. Create an admin user for cubarimoe.
```
python3 manage.py createsuperuser
```

## Start the server
-  `python3 manage.py runserver` - keep this console active

Now the site should be accessible on localhost:8000

## Other info
Relevant URLs (as of now): 

- `/` - home page
- `/admin` - admin view (login with created user above)
- `/admin_home` - admin endpoint for clearing the site's cache
