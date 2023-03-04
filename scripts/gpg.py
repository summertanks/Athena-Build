import gnupg

class gpg:

    def __init__(self):
        # create a GPG object
        self.gpg = gnupg.GPG()
        key = gpg.gen_key(self.gen_new())

        # export the key to a file
        with open("mykey.asc", "w") as f:
            f.write(gpg.export_keys(key))

    def gen_new(self):
        # generate a new key
        input_data = gpg.gen_key_input(
            key_type="RSA",
            key_length=2048,
            name_real="Your Name",
            name_email="you@example.com",
            passphrase="your_passphrase",
        )


