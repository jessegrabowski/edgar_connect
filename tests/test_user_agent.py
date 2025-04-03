import unittest
from unittest.mock import patch
from edgar_connect.user_agent import UserAgent


class TestUserAgent(unittest.TestCase):
    def test_user_agent_provided(self):
        user_agent_str = "TestCompany AdminContact@testcompany.com"
        user_agent = UserAgent(user_agent=user_agent_str)
        self.assertEqual(user_agent.update_user_agent(), user_agent_str)

    @patch("edgar_connect.user_agent.Faker")
    def test_user_agent_generated(self, mock_faker):
        mock_faker_instance = mock_faker.return_value
        mock_faker_instance.company.return_value = "MockCompany"
        mock_faker_instance.domain_name.return_value = "mockcompany.com"

        user_agent = UserAgent()
        generated_user_agent = user_agent.update_user_agent()
        self.assertEqual(
            generated_user_agent, "MockCompany AdminContact@mockcompany.com"
        )


if __name__ == "__main__":
    unittest.main()
