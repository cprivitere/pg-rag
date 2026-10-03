# Ideas to try

- major revamp of goldens
  - Add goldens by me asking you questions that you research and then turn into goldens
  - leverage the ability to call only certain types of goldens to reduce test time

- Track pricing of items in player stalls and player work orders

- sync scripts for game state
  - easily sync up latest data from game
  - Perhaps some sort of daemon or runner that kicks off at the start of any chat session or the start of chat?
  
- Look at glogger source code and incorporate things it does to gather/save data
  - alternately, just use the glogger sqlite as a primary source
    - would this overlap with the already created player data we're getting?

- Instead of a separate chat window web thing, package this in such a way that I can make a custom build of glogger that has this as a tab inside of it
  - first class access to glogger's data

- Figure out a way to publish the sqlite data privately (or do the same trick we do with the private verison of the full corpus) and do the inference/tool loops on a molab notebook


