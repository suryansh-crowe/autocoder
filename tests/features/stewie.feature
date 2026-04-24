Feature: Stewie AI chat prompt submission, role selection, and cross-page navigation
  Validates chat prompt submission and response, role selection effects, and navigation to Home with landmark assertion.

  Background:
    Given The user is on the Stewie AI page

  @smoke
  Scenario: Submitting a chat prompt returns a visible response area
    When The user enters 'List all assets within Finance domain.' in the chat prompt textbox
    And The user clicks the Send button
    Then A new conversation thread appears

  @regression @validation
  Scenario: Submitting an empty chat prompt disables the Send button
    When The user clears the chat prompt textbox
    Then The Send button becomes disabled

  @smoke
  Scenario: Selecting a role enables the chat prompt textbox
    When The user selects 'Data Analyst' from the Choose role dropdown
    Then The chat prompt textbox becomes enabled

  @regression @navigation
  Scenario: Navigating to Home displays the Stewie Terminal landmark
    When The user clicks the Home button
    Then The URL contains '/home'
    And The 'Stewie Terminal' heading is displayed
