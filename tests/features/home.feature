Feature: Stewie AI Home: Search, Chat, Navigation, and Action Consequences
  Validates search and chat submission, navigation to known pages, and action button consequences with concrete outcome assertions.

  Background:
    Given The user is on the Stewie AI Home page

  @smoke
  Scenario: Submitting asset search for 'DQ-AGT-001' displays matching result row
    When The user enters 'DQ-AGT-001' in the asset search box
    And The user submits the asset search
    Then The results area contains 'DQ-AGT-001'

  @regression @validation
  Scenario: Submitting asset search for unknown id shows no results message
    When The user enters 'unknown_id_42' in the asset search box
    And The user submits the asset search
    Then The results area displays 'No matching assets'

  @regression @validation
  Scenario: Submitting empty asset search keeps the result count unchanged
    When The user clears the asset search box
    And The user submits the asset search
    Then The results area shows the same number of rows as before

  @smoke
  Scenario: Submitting chat prompt 'Show asset rules for DQ-AGT-001' displays Stewie response
    When The user enters 'Show asset rules for DQ-AGT-001' in the Ask Stewie chat box
    And The user submits the chat prompt
    Then The chat response area displays a message containing 'DQ-AGT-001'

  @regression @validation
  Scenario: Submitting empty chat prompt shows validation error
    When The user clears the Ask Stewie chat box
    And The user submits the chat prompt
    Then A validation error message appears in the chat area

  @regression @navigation
  Scenario: Clicking Data Catalog navigates to Catalog page and displays Catalog landmark
    When The user clicks the Data Catalog button
    Then The URL contains '/catalog'
    And The Catalog page landmark is visible

  @regression @navigation
  Scenario: Clicking Ask Stewie navigates to Stewie page and displays Stewie Terminal heading
    When The user clicks the Ask Stewie button
    Then The URL contains '/stewie'
    And The 'Stewie Terminal' heading is displayed

  @regression @navigation
  Scenario: Clicking Source Connection navigates to Source Connection page and displays relevant heading
    When The user clicks the Source Connection button
    Then The Source Connection page landmark is visible

  @regression @navigation
  Scenario: Clicking Data Quality navigates to Data Quality page and displays relevant heading
    When The user clicks the Data Quality button
    Then The Data Quality page landmark is visible

  @regression @navigation
  Scenario: Clicking Agent Pipelines navigates to Agent Pipelines page and displays relevant heading
    When The user clicks the Agent Pipelines button
    Then The Agent Pipelines page landmark is visible

  @regression @navigation
  Scenario: Clicking Agent Management navigates to Agent Management page and displays relevant heading
    When The user clicks the Agent Management button
    Then The Agent Management page landmark is visible

  @regression @navigation
  Scenario: Clicking Security navigates to Security page and displays relevant heading
    When The user clicks the Security button
    Then The Security page landmark is visible

  @regression @navigation
  Scenario: Clicking Notifications navigates to Notifications page and displays relevant heading
    When The user clicks the Notifications button
    Then The Notifications page landmark is visible

  @smoke
  Scenario: Clicking Close sidebar collapses the sidebar and enables main content
    When The user clicks the Close sidebar button
    Then The main content area becomes fully enabled
