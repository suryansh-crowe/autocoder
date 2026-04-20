Feature: Stewie AI Insights dashboard interaction
  Tests dashboard search, navigation, and action buttons on the Data Quality Insights page.

  Background:
    Given User is on the Data Quality Insights page

  @smoke
  Scenario: User searches for a dashboard with a typed query
    When User enters a dashboard name in the search box
    And User clicks the Filter button to submit the search
    Then The Dashboards heading is visible indicating search results

  @regression @validation
  Scenario: User submits an empty search query
    When User leaves the search box empty
    And User clicks the Filter button
    Then The Dashboards heading remains visible and no validation message appears

  @smoke
  Scenario: User navigates to the Dashboards section
    When User clicks the Dashboards navigation button
    Then The Dashboards heading is displayed

  @smoke
  Scenario: User clicks the Ask Stewie action button
    When User clicks the Ask Stewie button
    Then The Ask Stewie interface is displayed

  @smoke
  Scenario: User clicks the Data Catalog action button
    When User clicks the Data Catalog button
    Then The Data Quality heading is visible
