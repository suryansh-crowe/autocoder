Feature: Security Access Request
  Tests the access request form and navigation for the Security page.

  Background:
    Given User is on the Security page

  @smoke
  Scenario: Submit a valid access request
    When User clicks the Request Access button
    And User enters a reason for access
    And User clicks the Submit Request button
    Then The Your Request History section is visible

  @regression @validation
  Scenario: Submit access request with empty reason
    When User clicks the Request Access button
    And User leaves the reason for access empty
    And User clicks the Submit Request button
    Then A validation message for the reason field is displayed

  @smoke
  Scenario: Navigate to Home from Security page
    When User clicks the Home button
    Then The Home page heading is visible

  @smoke
  Scenario: Close the sidebar navigation
    When User clicks the Close sidebar button
    Then The sidebar is hidden

  @smoke
  Scenario: Access Data Catalog section
    When User clicks the Data Catalog button
    Then The Data Catalog section heading is visible

  @smoke
  Scenario: Access Agent Management section
    When User clicks the Agent Management button
    Then The Agent Management section heading is visible
