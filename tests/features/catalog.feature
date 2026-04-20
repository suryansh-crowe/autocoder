Feature: Catalog page core interactions
  Tests search, navigation, and action buttons on the Catalog page.

  Background:
    Given User is on the Catalog page

  @smoke
  Scenario: User searches for an asset and sees results indicator
    Given User enters a valid asset name in the search box
    When User clicks the Filter button
    Then Pagination controls are displayed

  @regression @validation
  Scenario: User submits an empty search and sees unfiltered pagination
    Given User leaves the search box empty
    When User clicks the Filter button
    Then Pagination controls remain unchanged

  @smoke
  Scenario: User navigates to Home and sees the Catalog heading
    When User clicks the Home button
    Then The Catalog heading is visible

  @smoke
  Scenario: User closes the sidebar and sees the Catalog heading
    When User clicks the Close sidebar button
    Then The Catalog heading is visible

  @smoke
  Scenario: User clicks Ask Stewie and sees the assistant panel
    When User clicks the Ask Stewie button
    Then The Stewie assistant panel is displayed

  @smoke
  Scenario: User clicks Data Quality and sees the Catalog heading
    When User clicks the Data Quality button
    Then The Catalog heading is visible
