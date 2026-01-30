<!--
 * @Author: lincker
 * @Date: 2025-04-28 14:21:21
 * @LastEditors: lincker haolin.d.wu@gmail.com
 * @LastEditTime: 2025-12-23 09:42:20
 * @FilePath: /OmniBCI/Readme.md
 * @Description: 
 * 
 * Copyright (c) 2025 by lincker, All Rights Reserved. 
-->
OmniBCI is designed to be a BCI system that is easy to use and deploy. 

1.It uses Python 3.12 and OpenGL for rendering some stimulis. The system is designed to be cross-platform, allowing it to run on Windows, macOS, and Linux.

2.It provides a user-friendly GUI for configuring and controlling the system. The GUI is designed to be intuitive and easy to use, allowing users to quickly set up and start using the system.

3.It is designed to support multiple devices for reading EEG data. 

4.It is designed to support multiple algorithms to analysis epoch data.

5.It is designed to support multiple paradigms by different stimulus.

6.It is designed to support multiple external devices.

```ascii
                                               +-----------------+       
                                               |   Peripherals   |
                                               +-----------------+ 
                                                        ^                     
                                                        |                     
                                                        |                     
    +---------------+       +-------------+       +-------------+       +--------------+
    | Data Provider |------>| Acquisition |------>| NeuroEngine |------>| Stimulation  |
    +---------------+       +-------------+       +-------------+       +--------------+
                                    ^                   ^                     ^
                                    |                   |                     |
                                    |                   |                     |
                                    +-------------------+---------------------+
                                                        |
                                                        |
                                                  +----------+
                                                  | Control  |
                                                  |   GUI    |
                                                  +----------+
```