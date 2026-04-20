Feature: Stewie AI main page interaction
  Covers chat, navigation, action buttons, and role selection validation.

  Background:
    Given User is on the Stewie AI main page

  @smoke
  Scenario: User sends a prompt to Stewie and receives a response
    Given User enters a prompt in the Ask Stewie message box
    When User clicks the Send button
    Then The Conversations panel is displayed

  @regression @navigation
  Scenario: User navigates to Data Catalog and sees the Data Catalog heading
    When User clicks the Data Catalog button
    Then The Data Catalog section is visible

  @smoke
  Scenario: User clicks the Ask Stewie button and sees the Ask Stewie message box
    When User clicks the Ask Stewie button
    Then The Ask Stewie message box is visible

  @regression @validation
  Scenario: User selects a role and the Send button becomes enabled
    When User selects a role from the Choose role dropdown
    Then The Send button is enabled

  @regression @validation
  Scenario: User attempts to send without selecting a role and sees a validation message
    When User clicks the Send button without selecting a role
    Then A validation message is displayed for role selection

  @smoke
  Scenario: User clicks the Notifications button and sees the Notifications panel
    When User clicks the Notifications button
    Then The Notifications panel is visible
