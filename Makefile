PYTHON	?= python3

all:
	@echo 'Nothing to build; bjoern is pure Python.'

test:
	$(PYTHON) -m unittest tests.test_server tests.test_parser -v
	$(PYTHON) tests/interrupt-during-request.py
	$(PYTHON) tests/keep-alive-behaviour.py
	$(PYTHON) tests/test_wsgi_compliance.py
	$(PYTHON) tests/expect100.py

AB		= ab -c 100 -n 10000
TEST_URL	= "http://127.0.0.1:8080/a/b/c?k=v&k2=v2"

ab: ab1 ab2 ab3 ab4

ab1:
	$(AB) $(TEST_URL)
ab2:
	@echo 'asdfghjkl=asdfghjkl&qwerty=qwertyuiop' > /tmp/bjoern-post.tmp
	$(AB) -p /tmp/bjoern-post.tmp $(TEST_URL)
ab3:
	$(AB) -k $(TEST_URL)
ab4:
	@echo 'asdfghjkl=asdfghjkl&qwerty=qwertyuiop' > /tmp/bjoern-post.tmp
	$(AB) -k -p /tmp/bjoern-post.tmp $(TEST_URL)

wget:
	wget -O - -q -S $(TEST_URL)

clean:
	@rm -rf build/ dist/ *.egg-info

packages:
	$(PYTHON) -m build

upload: packages
	twine upload dist/*.tar.gz dist/*.whl
