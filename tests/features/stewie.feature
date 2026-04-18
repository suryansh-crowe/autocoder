Feature: Stewie AI Sign-In and Terms Navigation
  Ensures users can sign in with Microsoft, agree to terms, and navigate privacy policy.

  Background:
    Given I am on the Stewie AI homepage

  @smoke
  Scenario: Smoke Test: Sign in with Microsoft and agree to terms
    When I click the sign-in button for Microsoft
    And I fill in my email address
    And I click the terms of service button

  @smoke
  Scenario: Happy Path: Complete sign-in and navigate to privacy policy
    When I click the sign-in button for Microsoft
    And I fill in my email address
    And I click the privacy policy button

  @regression @validation
  Scenario: Validation: Ensure terms of service and privacy policy buttons are clickable
    Then The terms of service button is visible
    And The privacy policy button is visible
