from faker import Faker


class UserAgent:
    """
    A class to generate a random user agent string. If a user agent is provided, it will always be used instead.
    """

    def __init__(self, user_agent: str | None = None):
        self.user_agent = user_agent
        self.fake = Faker()

    def update_user_agent(self):
        """
        Generate a User-Agent header in the form requested by the SEC. See here for details:

        https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
        """
        if self.user_agent is not None:
            return self.user_agent

        company = self.fake.company()
        domain = self.fake.domain_name()

        return f"{company} AdminContact@{domain}"
