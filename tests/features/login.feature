Feature: User Login and Agreement to Terms
  Ensure users can log in using Microsoft and agree to terms.

  Background:
    Given I am on the login page

  @smoke
  Scenario: Smoke Test: Successful Login with Microsoft
    When I click sign in with Microsoft
    Then I should be logged in successfully

  @smoke
  Scenario: Happy Path: Agree to Terms and Privacy Policy
    When I click terms of service
    And I click privacy policy

  @regression @validation
  Scenario: Validation: Enter Email and Agree to Terms
    When I fill in my email address
    And I click terms of service
