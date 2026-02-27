

void setup() {
  pinMode(7, OUTPUT); // Set pin 7 as an output
}

void loop() {
  digitalWrite(7, HIGH); // Turn the LED on
  delay(1000); // Wait for a second
  digitalWrite(7, LOW);  // Turn the LED off
  delay(1000); // Wait for a second
}

